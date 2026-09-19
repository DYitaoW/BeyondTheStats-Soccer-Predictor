import unittest
from datetime import date

class EspnSmartDatesTests(unittest.TestCase):
    def test_uefa_dates_are_tue_wed_thu(self):
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "shared"))
        import espn_api_cache as c

        days = c.iter_scoreboard_dates("uefa.europa", date(2026, 9, 1), date(2026, 9, 30))
        # Sept 2026 has 30 days; Tue/Wed/Thu subset should be well under 30.
        self.assertLess(len(days), 20)
        self.assertGreater(len(days), 8)
        for d in days:
            if d not in {date(2026, 9, 1), date(2026, 9, 30)}:
                self.assertIn(d.weekday(), {1, 2, 3})

    def test_domestic_cup_uses_all_days(self):
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "shared"))
        import espn_api_cache as c

        days = c.iter_scoreboard_dates("eng.fa", date(2026, 9, 1), date(2026, 9, 10))
        self.assertEqual(len(days), 10)

    def test_mls_upcoming_uses_espn_cache_not_multiday_range(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (root / "pipelines" / "mls" / "files" / "Predict_Upcoming_Matchweek.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("espn_api_cache.fetch_scoreboard_range", source)
        self.assertNotIn('f"?dates={today.strftime(\'%Y%m%d\')}-{end.strftime(\'%Y%m%d\')}&limit=1000"', source)
        self.assertIn("load_upcoming_matchweek_fixtures_from_raw_season_files", source)

    def test_website_schedule_uses_espn_cache(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (root / "Website" / "espn_api.py").read_text(encoding="utf-8")
        self.assertIn("espn_api_cache.fetch_scoreboard_range", source)
        self.assertNotIn("?dates={today_str}-{end}&limit=1000", source)

    def test_track_cups_seeds_pending_fixtures(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (
            root / "pipelines" / "europe" / "files" / "Track_Cup_Results.py"
        ).read_text(encoding="utf-8")
        self.assertIn("pending_seeded", source)
        self.assertIn("fetch_upcoming_cup_table_fixtures(shared_mapping)", source)

    def test_friendlies_sync_includes_international(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (
            root / "pipelines" / "europe" / "files" / "Update_Club_Friendlies.py"
        ).read_text(encoding="utf-8")
        self.assertIn("fifa.friendly", source)
        self.assertIn("International/Friendly", source)


if __name__ == "__main__":
    unittest.main()
