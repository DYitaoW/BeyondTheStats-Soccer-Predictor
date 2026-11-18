"""Automated test suite for NWSL and WSL womens leagues integration."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# This suite inspects real data (women's fixtures, league data). It runs
# against a snapshot COPY of the production store, so anything the endpoints
# write (projection snapshots, caches) lands in the copy. It used to set
# BTS_ALLOW_REAL_STORE_IN_TESTS=1 at import time, which disabled store
# isolation for the WHOLE test run (discovery imports every module first), so
# other suites wrote fake devices, feed events and fixtures into production.

# Setup paths
TEST_DIR = Path(__file__).resolve().parent
APP_DIR = TEST_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(APP_DIR / "Website") not in sys.path:
    sys.path.insert(0, str(APP_DIR / "Website"))
if str(APP_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(APP_DIR / "shared"))

from starlette.testclient import TestClient

import config
from competition_rules import (
    canonical_team_name,
    resolve_competition_query,
    get_league_qualification_zones,
    competition_format_spec,
)
from standings import _canonical_roster_teams, compute_league_form_table
from shared import sqlite_store as store
from app import app


class TestWomensLeaguesIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        import sqlite3
        import tempfile

        from shared import paths as _paths

        tmp_dir = tempfile.mkdtemp(prefix="bts-womens-store-")
        cls.addClassCleanup(shutil.rmtree, tmp_dir, True)
        snapshot = Path(tmp_dir) / "bts_store.db"
        src = sqlite3.connect(f"file:{_paths.SQLITE_STORE_FILE}?mode=ro", uri=True)
        dst = sqlite3.connect(str(snapshot))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        cls._store_env = mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(snapshot)})
        cls._store_env.start()
        cls.addClassCleanup(cls._store_env.stop)
        from competition_rules import warm_competition_games_cache
        warm_competition_games_cache(force=True)
        cls.client = TestClient(app)

    def test_live_polling_config(self):
        """Verify NWSL and WSL are registered for ESPN live polling."""
        self.assertIn("United States/NWSL", config.LIVE_SCORE_COMPETITIONS)
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["United States/NWSL"], "usa.nwsl")

        self.assertIn("England/Women's Super League", config.LIVE_SCORE_COMPETITIONS)
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["England/Women's Super League"], "eng.w.1")

        self.assertIn("United States/NWSL", config.CORE_LIVE_POLL_COMPETITIONS)
        self.assertIn("England/Women's Super League", config.CORE_LIVE_POLL_COMPETITIONS)

    def test_team_canonicalization_and_isolation(self):
        """Verify team name resolution and isolation between men's and women's clubs."""
        # WSL resolution
        wsl_comp = "England/Women's Super League"
        self.assertEqual(canonical_team_name("Arsenal", wsl_comp), "Arsenal Women")
        self.assertEqual(canonical_team_name("Chelsea", wsl_comp), "Chelsea Women")
        self.assertEqual(canonical_team_name("Man City", wsl_comp), "Manchester City Women")
        self.assertEqual(canonical_team_name("Manchester United", wsl_comp), "Manchester United Women")

        # Premier League isolation (Men's clubs should not be modified to Women's)
        pl_comp = "England/Premier League"
        self.assertEqual(canonical_team_name("Arsenal", pl_comp), "Arsenal")
        self.assertEqual(canonical_team_name("Chelsea", pl_comp), "Chelsea")

        # NWSL resolution
        nwsl_comp = "United States/NWSL"
        self.assertEqual(canonical_team_name("Gotham FC", nwsl_comp), "NJ/NY Gotham FC")
        self.assertEqual(canonical_team_name("Angel City", nwsl_comp), "Angel City FC")
        self.assertEqual(canonical_team_name("Kansas City", nwsl_comp), "Kansas City Current")
        self.assertEqual(canonical_team_name("Courage", nwsl_comp), "North Carolina Courage")

    def test_competition_rules_and_qualification_zones(self):
        """Verify competition specs and qualification zones for NWSL and WSL."""
        # NWSL Zones: 1st Shield, 2-8 Playoffs
        nwsl_zones = get_league_qualification_zones("United States/NWSL")
        self.assertIsNotNone(nwsl_zones)
        self.assertEqual(len(nwsl_zones), 2)
        self.assertEqual(nwsl_zones[0]["positions"], [1])
        self.assertEqual(nwsl_zones[0]["short_name"], "Shield")
        self.assertEqual(nwsl_zones[1]["positions"], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(nwsl_zones[1]["short_name"], "Playoffs")

        # WSL Zones: 1st UWCL Group, 2-3 UWCL Q, 12th Relegation
        wsl_zones = get_league_qualification_zones("England/Women's Super League")
        self.assertIsNotNone(wsl_zones)
        self.assertEqual(len(wsl_zones), 3)  # 14 clubs from 2026-27
        self.assertEqual(wsl_zones[0]["positions"], [1])
        self.assertEqual(wsl_zones[0]["type"], "champions_league")
        self.assertEqual(wsl_zones[1]["positions"], [2, 3])
        self.assertEqual(wsl_zones[1]["type"], "champions_league_qual")
        self.assertEqual(wsl_zones[2]["positions"], [14])
        self.assertEqual(wsl_zones[2]["type"], "relegation")

        # Formats
        nwsl_spec = competition_format_spec("United States/NWSL")
        self.assertEqual(nwsl_spec.get("playoff_format"), "nwsl_playoffs")
        self.assertEqual(nwsl_spec.get("playoff_qualifiers"), 8)

        wsl_spec = competition_format_spec("England/Women's Super League")
        self.assertEqual(wsl_spec.get("relegation_spots"), 1)

    def test_canonical_rosters(self):
        """Verify all clubs exist in canonical roster definitions."""
        # Real 2026 / 2026-27 rosters (ESPN): NWSL added Boston Legacy and
        # Denver Summit; WSL has 14 clubs incl. promoted Birmingham, Charlton
        # and London City Lionesses (Leicester gone).
        nwsl_teams = _canonical_roster_teams("United States/NWSL")
        self.assertEqual(len(nwsl_teams), 16)
        for team in ("Orlando Pride", "NJ/NY Gotham FC", "Boston Legacy FC", "Denver Summit FC"):
            self.assertIn(team, nwsl_teams)

        wsl_teams = _canonical_roster_teams("England/Women's Super League")
        self.assertEqual(len(wsl_teams), 14)
        for team in ("Chelsea Women", "Arsenal Women", "London City Lionesses", "Birmingham City Women",
                     "Charlton Athletic Women"):
            self.assertIn(team, wsl_teams)
        self.assertNotIn("Leicester City Women", wsl_teams)

    def test_form_table_calculation(self):
        """Verify dynamic form table calculation for NWSL and WSL."""
        nwsl_form = compute_league_form_table("United States/NWSL", window=5)
        self.assertEqual(nwsl_form.get("competition"), "United States/NWSL")
        self.assertEqual(len(nwsl_form.get("overall", [])), 16)

        wsl_form = compute_league_form_table("England/Women's Super League", window=5)
        self.assertEqual(wsl_form.get("competition"), "England/Women's Super League")
        self.assertEqual(len(wsl_form.get("overall", [])), 14)

        # Check entry fields
        entry = nwsl_form["overall"][0]
        for field in ("team", "P", "W", "D", "L", "GF", "GA", "GD", "Pts", "ppg", "form", "qualification"):
            self.assertIn(field, entry)

    def test_sqlite_persistence_and_statistical_parity(self):
        """Verify SQLite past_games and upcoming_games parity fields."""
        # Past games
        all_past = store.load_past_games()
        past_nwsl = [g for g in all_past if g.get("competition") == "United States/NWSL"]
        past_wsl = [g for g in all_past if g.get("competition") == "England/Women's Super League"]
        self.assertGreater(len(past_nwsl), 0)
        self.assertGreater(len(past_wsl), 0)
        self.assertIn("actual_result", past_nwsl[0])
        self.assertIn("actual_home_goals", past_nwsl[0])
        # No rows from the old synthetic generator (source "womens") remain.
        womens_past = past_nwsl + past_wsl
        self.assertFalse([g for g in womens_past if g.get("source") == "womens"])

        # Upcoming games
        upcoming = store.load_upcoming_games()
        nwsl_up = [g for g in upcoming if g.get("competition") == "United States/NWSL"]
        wsl_up = [g for g in upcoming if g.get("competition") == "England/Women's Super League"]
        self.assertGreater(len(nwsl_up), 0)
        self.assertGreater(len(wsl_up), 0)

        # Real fixtures: every prediction has the ESPN kickoff time and match id.
        for g in nwsl_up + wsl_up:
            self.assertTrue(g.get("kickoff_utc"), g.get("sync_key"))
            self.assertTrue(g.get("match_id"), g.get("sync_key"))

        # Verify 79+ statistical parity keys in sample upcoming fixture
        sample = (nwsl_up + wsl_up)[0]
        parity_keys = [
            "prob_home", "prob_draw", "prob_away",
            "pred_home_goals", "pred_away_goals",
            "pred_home_shots", "pred_away_shots",
            "pred_home_sot", "pred_away_sot",
            "pred_home_possession", "pred_away_possession",
            "pred_home_corners", "pred_away_corners",
            "pred_home_fouls", "pred_away_fouls",
            "pred_home_cards", "pred_away_cards",
            "correct_score_dist", "total_goals_dist",
            "first_to_score", "clean_sheet", "double_chance",
            "xpts_home", "xpts_away", "expected_points",
            "prob_both_score", "prob_over_1_5", "prob_over_2_5", "prob_over_3_5",
            "last_5_home", "last_5_away",
            "odds_home", "odds_draw", "odds_away",
            "winner_label", "has_prediction", "prediction_quality",
        ]
        for key in parity_keys:
            self.assertIn(key, sample, f"Missing parity key: {key}")

    def test_api_endpoints(self):
        """Verify API endpoints return valid JSON for womens leagues."""
        # Upcoming womens endpoint
        resp = self.client.get("/api/upcoming/womens")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertGreater(data.get("total", 0), 0)

        # NWSL league data
        resp_nwsl = self.client.get("/api/league-data/United States/NWSL")
        self.assertEqual(resp_nwsl.status_code, 200)
        nwsl_data = resp_nwsl.json()
        self.assertTrue(nwsl_data.get("ok"))
        self.assertEqual(len(nwsl_data.get("predicted_table", [])), 16)
        self.assertIsNotNone(nwsl_data.get("qualification_zones"))
        self.assertGreater(len(nwsl_data.get("winners_odds", [])), 0)

        # WSL league data
        resp_wsl = self.client.get("/api/league-data/England/Women's Super League")
        self.assertEqual(resp_wsl.status_code, 200)
        wsl_data = resp_wsl.json()
        self.assertTrue(wsl_data.get("ok"))
        self.assertEqual(len(wsl_data.get("predicted_table", [])), 14)
        self.assertIsNotNone(wsl_data.get("qualification_zones"))
        self.assertGreater(len(wsl_data.get("winners_odds", [])), 0)


if __name__ == "__main__":
    unittest.main()
