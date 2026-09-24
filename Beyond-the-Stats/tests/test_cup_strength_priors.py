"""Tests for UEFA/cup domestic×coefficient strength priors."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
ROOT_DIR = WEBSITE_DIR.parent
FILES_DIR = ROOT_DIR / "pipelines" / "europe" / "files"
for path in (WEBSITE_DIR, FILES_DIR, ROOT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class CupStrengthPriorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uefa = importlib.import_module("UEFA_Data_Manager")

    def test_stronger_league_favorite_beats_weaker_champion(self):
        # Mid-table Prem side should outrank a weak-league champion on coeff×pos.
        with mock.patch.object(
            self.uefa,
            "lookup_team_data_for_fallback",
            side_effect=lambda team, **kwargs: (
                {
                    "country": "England",
                    "league": "England/Premier League",
                    "league_strength": 1.00,
                    "squad_value_eur_m": 400.0,
                    "domestic": {"position": 8, "points": 20, "played": 10},
                    "domestic_ppg": 2.0,
                }
                if "City" in team or team == "MidPrem"
                else {
                    "country": "Faroe Islands",
                    "league": "Faroe Islands/Premier League",
                    "league_strength": 0.45,
                    "squad_value_eur_m": 5.0,
                    "domestic": {"position": 1, "points": 30, "played": 10},
                    "domestic_ppg": 3.0,
                }
            ),
        ):
            prior = self.uefa.cup_matchup_prior("MidPrem", "WeakChamp")
        self.assertIsNotNone(prior)
        self.assertGreater(prior["H"], prior["A"])
        self.assertGreater(prior["H"], 0.45)
        self.assertLess(prior["D"], 0.35)

    def test_blend_moves_equal_model_toward_prior(self):
        model = {"H": 0.34, "D": 0.33, "A": 0.33}
        prior = {"H": 0.62, "D": 0.22, "A": 0.16}
        blended = self.uefa.blend_probs_with_cup_prior(model, prior, prior_weight=0.5)
        self.assertGreater(blended["H"], model["H"])
        self.assertAlmostEqual(sum(blended.values()), 1.0, places=5)

    def test_project_uefa_uses_strength_path(self):
        src = (FILES_DIR / "Project_UEFA_Cups.py").read_text(encoding="utf-8")
        self.assertIn("cup_matchup_prior", src)
        self.assertIn("_sync_cups_to_sqlite", src)
        self.assertIn("Predict_Upcoming_Cups", src)

    def test_sqlite_store_importable(self):
        from shared import sqlite_store

        self.assertTrue(callable(sqlite_store.upsert_past_games))
        self.assertTrue(callable(sqlite_store.upsert_upcoming_games))
        info = sqlite_store.store_info()
        self.assertIn("path", info)


if __name__ == "__main__":
    unittest.main()
