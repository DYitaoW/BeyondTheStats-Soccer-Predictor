"""Tests for league qualification zones, weekly historical comparisons, and form tables."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure Beyond-the-Stats and Website are on sys.path
_TEST_DIR = Path(__file__).resolve().parent
_BTS_DIR = _TEST_DIR.parent
_WEBSITE_DIR = _BTS_DIR / "Website"
if str(_BTS_DIR) not in sys.path:
    sys.path.insert(0, str(_BTS_DIR))
if str(_WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBSITE_DIR))

from competition_rules import (
    get_league_qualification_zones,
    get_qualification_for_position,
    resolve_competition_query,
)
from espn_api import _parse_standings_entry
from league_data import (
    _enrich_league_data_comparison,
    build_league_data_payload,
    clear_league_data_caches,
)
from shared import sqlite_store
from standings import compute_league_form_table
from team_utils import decorate_standings_entry, decorate_standings_table


class TestLeagueFeatures(unittest.TestCase):
    """Test qualification zones, ESPN rules parsing, projection history snapshots, and league form tables."""

    def test_qualification_zones_definitions(self):
        """Test canonical qualification zones across top domestic leagues and continental tournaments."""
        # Premier League
        epl_zones = get_league_qualification_zones("England/Premier League")
        self.assertTrue(len(epl_zones) >= 4)
        ucl = next((z for z in epl_zones if z["type"] == "champions_league"), None)
        self.assertIsNotNone(ucl)
        self.assertEqual(ucl["positions"], [1, 2, 3, 4])
        rel = next((z for z in epl_zones if z["type"] == "relegation"), None)
        self.assertIsNotNone(rel)
        self.assertEqual(rel["positions"], [18, 19, 20])

        # Bundesliga (includes relegation play-off at 16)
        bdf_zones = get_league_qualification_zones("Germany/Bundesliga")
        rel_po = next((z for z in bdf_zones if z["type"] == "relegation_playoffs"), None)
        self.assertIsNotNone(rel_po)
        self.assertEqual(rel_po["positions"], [16])

        # UEFA Champions League 36-team single table
        ucl_comp_zones = get_league_qualification_zones("Europe/Champions League")
        r16 = next((z for z in ucl_comp_zones if z["type"] == "round_of_16"), None)
        self.assertIsNotNone(r16)
        self.assertEqual(r16["positions"], list(range(1, 9)))
        elim = next((z for z in ucl_comp_zones if z["type"] == "elimination"), None)
        self.assertIsNotNone(elim)
        self.assertEqual(elim["positions"], list(range(25, 37)))

        # MLS conference vs supporters shield
        mls_east_zones = get_league_qualification_zones("United States/MLS - Eastern Conference")
        po = next((z for z in mls_east_zones if z["type"] == "playoffs"), None)
        self.assertIsNotNone(po)
        self.assertEqual(po["positions"], list(range(1, 8)))

        mls_shield_zones = get_league_qualification_zones("United States/MLS - Supporters Shield Table")
        shield = next((z for z in mls_shield_zones if z["type"] == "champions_league"), None)
        self.assertIsNotNone(shield)
        self.assertEqual(shield["positions"], [1])

    def test_get_qualification_for_position_and_decoration(self):
        """Test position-level qualification lookup and row decoration."""
        # Top 4 in EPL
        q1 = get_qualification_for_position("England/Premier League", 1)
        self.assertIsNotNone(q1)
        self.assertEqual(q1["type"], "champions_league")
        self.assertEqual(q1["short_name"], "UCL")

        # 5th in EPL -> UEL
        q5 = get_qualification_for_position("England/Premier League", 5)
        self.assertIsNotNone(q5)
        self.assertEqual(q5["type"], "europa_league")

        # 19th in EPL -> Relegation
        q19 = get_qualification_for_position("England/Premier League", 19)
        self.assertIsNotNone(q19)
        self.assertEqual(q19["type"], "relegation")

        # Mid-table (10th) in EPL -> None
        q10 = get_qualification_for_position("England/Premier League", 10)
        self.assertIsNone(q10)

        # Decoration on row dict
        entry = {"team": "Arsenal", "position": 1}
        decorate_standings_entry(entry, "England/Premier League")
        self.assertIn("qualification", entry)
        self.assertEqual(entry["qualification"]["type"], "champions_league")
        self.assertEqual(entry["team_id"], "arsenal")

        # Decoration on table with groups
        table_data = {
            "groups": [
                {
                    "name": "Overall",
                    "entries": [
                        {"team": "Man City", "position": 2},
                        {"team": "Everton", "position": 18},
                    ],
                }
            ]
        }
        decorate_standings_table(table_data, "England/Premier League")
        e0 = table_data["groups"][0]["entries"][0]
        e1 = table_data["groups"][0]["entries"][1]
        self.assertEqual(e0["qualification"]["type"], "champions_league")
        self.assertEqual(e1["qualification"]["type"], "relegation")

    def test_espn_note_parsing(self):
        """Test ESPN standings parser extracts note and maps to qualification."""
        raw_espn_entry = {
            "team": {"displayName": "Paris Saint-Germain", "id": "160"},
            "stats": [{"name": "rank", "value": 1}, {"name": "points", "value": 24}],
            "note": {
                "color": "#004682",
                "description": "Champions League direct",
                "rank": 1,
            },
        }
        parsed = _parse_standings_entry(raw_espn_entry)
        self.assertIn("note", parsed)
        self.assertEqual(parsed["note"]["description"], "Champions League direct")
        self.assertIn("qualification", parsed)
        self.assertEqual(parsed["qualification"]["name"], "Champions League direct")
        self.assertEqual(parsed["qualification"]["color"], "#004682")

    def test_sqlite_projection_snapshot_storage(self):
        """Test saving, loading, and series retrieval of projection snapshots in SQLite."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_store.db"

            # 1. Save snapshot 1 (T-7)
            ok1 = sqlite_store.save_league_projection_snapshot(
                competition="Spain/La Liga",
                season="2026-27",
                winner_odds=[{"team": "Real Madrid", "win_league_pct": 55.0}, {"team": "Barcelona", "win_league_pct": 35.0}],
                table_rows=[{"team": "Real Madrid", "position": 1}, {"team": "Barcelona", "position": 2}],
                snapshot_date="2026-11-01",
                db_path=db_path,
            )
            self.assertTrue(ok1)

            # 2. Save snapshot 2 (T)
            ok2 = sqlite_store.save_league_projection_snapshot(
                competition="Spain/La Liga",
                season="2026-27",
                winner_odds=[{"team": "Real Madrid", "win_league_pct": 52.0}, {"team": "Barcelona", "win_league_pct": 38.0}],
                table_rows=[{"team": "Real Madrid", "position": 1}, {"team": "Barcelona", "position": 2}],
                snapshot_date="2026-11-08",
                db_path=db_path,
            )
            self.assertTrue(ok2)

            # 3. Load previous snapshot prior to 2026-11-08
            prev = sqlite_store.load_previous_projection_snapshot(
                competition="Spain/La Liga",
                before_date="2026-11-08",
                db_path=db_path,
            )
            self.assertIsNotNone(prev)
            self.assertEqual(prev["snapshot_date"], "2026-11-01")
            self.assertEqual(len(prev["winner_odds"]), 2)
            self.assertEqual(prev["winner_odds"][0]["team"], "Real Madrid")

            # 4. Series history
            series = sqlite_store.load_projection_history_series(
                competition="Spain/La Liga",
                limit=10,
                db_path=db_path,
            )
            self.assertEqual(len(series), 2)
            self.assertEqual(series[0]["snapshot_date"], "2026-11-01")
            self.assertEqual(series[1]["snapshot_date"], "2026-11-08")

    def test_enrich_league_data_comparison_calculations(self):
        """Test delta calculations for winner probabilities and table positions."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_store.db"

            sqlite_store.save_league_projection_snapshot(
                competition="Italy/Serie A",
                season="2026-27",
                winner_odds=[
                    {"team": "Inter", "win_league_pct": 60.0},
                    {"team": "Juventus", "win_league_pct": 20.0},
                    {"team": "Milan", "win_league_pct": 10.0},
                ],
                table_rows=[
                    {"team": "Inter", "position": 1},
                    {"team": "Juventus", "position": 2},
                    {"team": "Milan", "position": 3},
                ],
                snapshot_date="2026-11-01",
                db_path=db_path,
            )

            current_payload = {
                "ok": True,
                "competition": "Italy/Serie A",
                "winners_odds": [
                    {"team": "Inter", "win_league_pct": 54.0},     # -6.0%
                    {"team": "Juventus", "win_league_pct": 25.0},  # +5.0%
                    {"team": "Milan", "win_league_pct": 12.0},     # +2.0%
                ],
                "predicted_table": [
                    {"team": "Juventus", "position": 1},           # +1 improvement
                    {"team": "Inter", "position": 2},              # -1 drop
                    {"team": "Milan", "position": 3},              # 0 change
                ],
                "predicted": {},
            }

            prev_snapshot = sqlite_store.load_previous_projection_snapshot(
                "Italy/Serie A", before_date="2026-11-08", db_path=db_path
            )

            import unittest.mock as mock
            with mock.patch("shared.sqlite_store.load_previous_projection_snapshot", return_value=prev_snapshot):
                enriched = _enrich_league_data_comparison("Italy/Serie A", current_payload)

            comp = enriched["comparison"]
            self.assertTrue(comp["has_previous"])
            self.assertEqual(comp["previous_snapshot_date"], "2026-11-01")

            # Check Juventus is top gainer
            gainers = comp["biggest_gainers"]
            self.assertTrue(len(gainers) >= 1)
            self.assertEqual(gainers[0]["team"], "Juventus")
            self.assertEqual(gainers[0]["win_pct_change"], 5.0)
            self.assertEqual(gainers[0]["pos_change"], 1)

            # Check Inter is top faller
            fallers = comp["biggest_fallers"]
            self.assertTrue(len(fallers) >= 1)
            self.assertEqual(fallers[0]["team"], "Inter")
            self.assertEqual(fallers[0]["win_pct_change"], -6.0)
            self.assertEqual(fallers[0]["pos_change"], -1)

    def test_compute_league_form_table(self):
        """Test form table generation across overall, home, and away splits."""
        form_data = compute_league_form_table("England/Premier League", window=5)
        self.assertEqual(form_data["competition"], "England/Premier League")
        self.assertEqual(form_data["window"], 5)
        self.assertIn("overall", form_data)
        self.assertIn("home", form_data)
        self.assertIn("away", form_data)

        overall = form_data["overall"]
        self.assertTrue(len(overall) > 0)
        # Verify rank ordering: row i points >= row i+1 points or tiebroken by GD/GF
        for i in range(len(overall) - 1):
            r1 = overall[i]
            r2 = overall[i + 1]
            self.assertTrue(r1["Pts"] >= r2["Pts"])
            if r1["Pts"] == r2["Pts"]:
                self.assertTrue(r1["GD"] >= r2["GD"])

        # Row schema verification
        row0 = overall[0]
        self.assertEqual(row0["rank"], 1)
        self.assertEqual(row0["position"], 1)
        self.assertIn("team", row0)
        self.assertIn("team_id", row0)
        self.assertIn("team_logo", row0)
        self.assertEqual(row0["Pts"], row0["W"] * 3 + row0["D"])
        self.assertEqual(row0["GD"], row0["GF"] - row0["GA"])
        self.assertTrue(len(row0["form"]) <= 5)
        self.assertEqual(len(row0["form"]), row0["P"])
        if row0["P"] > 0:
            self.assertAlmostEqual(row0["ppg"], round(row0["Pts"] / row0["P"], 2))

    def test_build_league_data_payload_integration(self):
        """Test build_league_data_payload returns qualification zones, form table, and comparison."""
        clear_league_data_caches()
        payload = build_league_data_payload("England/Premier League")

        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("competition"), "England/Premier League")

        # 1. Qualification Zones
        self.assertIn("qualification_zones", payload)
        self.assertTrue(len(payload["qualification_zones"]) >= 4)
        self.assertEqual(payload["predicted"]["qualification_zones"], payload["qualification_zones"])

        # 2. Form Table
        self.assertIn("form_table", payload)
        form_tbl = payload["form_table"]
        self.assertEqual(form_tbl["window"], 5)
        self.assertTrue(len(form_tbl["overall"]) > 0)

        # 3. Comparison
        self.assertIn("comparison", payload)
        self.assertIn("odds_comparison", payload["comparison"])


if __name__ == "__main__":
    unittest.main()
