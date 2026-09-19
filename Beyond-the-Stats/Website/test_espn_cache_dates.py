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


if __name__ == "__main__":
    unittest.main()
