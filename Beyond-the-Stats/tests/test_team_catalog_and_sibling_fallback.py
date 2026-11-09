"""Tests for sibling-league fallback by country, team catalog deduplication, and SQLite store cleanup."""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import sqlite3

# Ensure Beyond-the-Stats and Website are on sys.path
_TEST_DIR = Path(__file__).resolve().parent
_BTS_DIR = _TEST_DIR.parent
_WEBSITE_DIR = _BTS_DIR / "Website"
if str(_BTS_DIR) not in sys.path:
    sys.path.insert(0, str(_BTS_DIR))
if str(_WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBSITE_DIR))

from competition_rules import canonical_team_name
from team_mappings import build_app_teams_catalog_payload
from shared import sqlite_store


class TestTeamCatalogAndSiblingFallback(unittest.TestCase):
    """Test sibling-league fallback by country, catalog deduplication, and SQLite cleanup."""

    def test_sibling_league_fallback_by_country(self):
        """Domestic cups should resolve team aliases using sibling domestic leagues of the same country."""
        # England (FA Cup / League Cup fall back to Championship / Premier League)
        self.assertEqual(canonical_team_name("Derby County", "England/FA Cup"), "Derby")
        self.assertEqual(canonical_team_name("Newcastle United", "England/League Cup"), "Newcastle")
        self.assertEqual(canonical_team_name("Bolton Wanderers", "England/FA Cup"), "Bolton")

        # Italy (Coppa Italia falls back to Serie A / Serie B)
        self.assertEqual(canonical_team_name("AS Roma", "Italy/Coppa Italia"), "Roma")

        # Germany (DFB-Pokal falls back to Bundesliga / Bundesliga 2)
        self.assertEqual(canonical_team_name("Bayer Leverkusen", "Germany/DFB-Pokal"), "Leverkusen")
        self.assertEqual(canonical_team_name("Eintracht Frankfurt", "Germany/DFB-Pokal"), "Ein Frankfurt")

        # Spain (Copa del Rey falls back to La Liga)
        self.assertEqual(canonical_team_name("Real Betis", "Spain/Copa del Rey"), "Betis")
        self.assertEqual(canonical_team_name("Athletic Club", "Spain/Copa del Rey"), "Ath Bilbao")

    def test_no_global_cross_country_leakage(self):
        """Cross-country team names should remain strictly isolated to avoid incorrect mappings."""
        # Inter Miami in MLS vs Inter (Internazionale) in Serie A
        self.assertEqual(canonical_team_name("Inter Miami", "United States/MLS"), "Inter Miami")
        self.assertEqual(canonical_team_name("Inter", "United States/MLS"), "Inter Miami")
        self.assertEqual(canonical_team_name("Inter", "Italy/Serie A"), "Inter")

        # A team unique to England should not map if requested under an Italian or German competition
        self.assertEqual(canonical_team_name("Derby", "Italy/Serie A"), "Derby")

    def test_catalog_payload_deduplication(self):
        """build_app_teams_catalog_payload should return zero duplicate team_ids and merge competitions."""
        payload = build_app_teams_catalog_payload()
        self.assertTrue(payload.get("ok"))
        self.assertGreater(payload.get("team_count", 0), 500)

        teams_with_comps = payload.get("teams_with_competitions", [])
        self.assertEqual(len(teams_with_comps), payload.get("team_count"))

        # Verify zero duplicate team_ids
        seen_ids = set()
        duplicates = []
        for t in teams_with_comps:
            tid = t.get("team_id")
            if tid:
                if tid in seen_ids:
                    duplicates.append(tid)
                seen_ids.add(tid)
        self.assertEqual(duplicates, [], f"Found duplicate team_ids in catalog: {duplicates}")

        # Verify Derby has both Championship and FA Cup merged
        derby_entry = next((t for t in teams_with_comps if t.get("team") == "Derby"), None)
        self.assertIsNotNone(derby_entry)
        self.assertIn("England/Championship", derby_entry.get("competitions", []))
        self.assertIn("England/FA Cup", derby_entry.get("competitions", []))

    def test_sqlite_cleanup_canonical_team_names(self):
        """cleanup_canonical_team_names should normalize home_team and away_team in SQLite store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_store.db"
            sqlite_store.ensure_store(test_db)

            # Insert raw non-canonical fixtures
            raw_rows = [
                {
                    "match_date_iso": "2026-11-15",
                    "competition": "England/FA Cup",
                    "home_team": "Derby County",
                    "away_team": "Newcastle United",
                    "status": "upcoming",
                    "source": "cups",
                },
                {
                    "match_date_iso": "2026-11-20",
                    "competition": "Italy/Coppa Italia",
                    "home_team": "AS Roma",
                    "away_team": "Hellas Verona",
                    "status": "upcoming",
                    "source": "cups",
                },
            ]
            sqlite_store.upsert_upcoming_games(raw_rows, db_path=test_db)

            # Verify un-canonicalized rows were inserted
            loaded = sqlite_store.load_upcoming_games(db_path=test_db)
            self.assertEqual(len(loaded), 2)

            # Run cleanup
            stats = sqlite_store.cleanup_canonical_team_names(db_path=test_db)
            self.assertEqual(stats["upcoming_updated"], 2)

            # Verify canonicalization
            cleaned = sqlite_store.load_upcoming_games(db_path=test_db)
            self.assertEqual(len(cleaned), 2)
            teams_in_db = {(r.get("home_team"), r.get("away_team")) for r in cleaned}
            self.assertIn(("Derby", "Newcastle"), teams_in_db)
            self.assertIn(("Roma", "Verona"), teams_in_db)


if __name__ == "__main__":
    unittest.main()
