"""Unit tests for UEFA CL/EL/ECL league-phase knockout helpers."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
ROOT_DIR = WEBSITE_DIR.parent
FILES_DIR = ROOT_DIR / "pipelines" / "europe" / "files"
for path in (WEBSITE_DIR, FILES_DIR, ROOT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class UEFACupKnockoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ko = importlib.import_module("UEFA_Cup_Knockout")

    def test_phase_match_caps(self):
        self.assertEqual(self.ko.UEFA_PHASE_MATCHES["Europe/Champions League"], 8)
        self.assertEqual(self.ko.UEFA_PHASE_MATCHES["Europe/Europa League"], 8)
        self.assertEqual(self.ko.UEFA_PHASE_MATCHES["Europe/Conference League"], 6)

    def test_playoff_draw_bands_pair_correct_seeds(self):
        ranked = [f"T{i}" for i in range(1, 25)]
        rng = np.random.default_rng(42)
        ties = self.ko.draw_knockout_playoff_ties(ranked, rng)
        self.assertEqual(len(ties), 8)
        by_band = {0: [], 1: [], 2: [], 3: []}
        for tie in ties:
            by_band[tie["band"]].append((tie["high_pos"], tie["low_pos"]))
            # Leg 1: lower seed hosts; leg 2: higher seed hosts.
            self.assertEqual(tie["leg1_home"], tie["low_team"])
            self.assertEqual(tie["leg2_home"], tie["high_team"])
        self.assertEqual(sorted(p[0] for p in by_band[0]), [9, 10])
        self.assertEqual(sorted(p[1] for p in by_band[0]), [23, 24])
        self.assertEqual(sorted(p[0] for p in by_band[1]), [11, 12])
        self.assertEqual(sorted(p[1] for p in by_band[1]), [21, 22])
        self.assertEqual(sorted(p[0] for p in by_band[2]), [13, 14])
        self.assertEqual(sorted(p[1] for p in by_band[2]), [19, 20])
        self.assertEqual(sorted(p[0] for p in by_band[3]), [15, 16])
        self.assertEqual(sorted(p[1] for p in by_band[3]), [17, 18])

    def test_two_legged_pens_on_aggregate_tie(self):
        rng = np.random.default_rng(7)

        def predict_fn(home, away, _rng):
            # Always 1-1 so aggregate is always level → penalties.
            return 1, 1, {"predicted_result": "D"}

        result = self.ko.resolve_two_legged_tie("High", "Low", rng, predict_fn)
        self.assertTrue(result["decided_by_penalties"])
        self.assertEqual(result["high_agg"], result["low_agg"])
        self.assertIn(result["winner"], {"High", "Low"})
        self.assertEqual(result["leg2"]["home"], "High")

    def test_r16_halves_keep_1_and_2_apart(self):
        ranked = [f"Seed{i}" for i in range(1, 25)]
        rng = np.random.default_rng(99)

        def predict_fn(home, away, _rng):
            # Always favour the "home" side of each sample (higher seed in leg 2).
            return 2, 0, {"predicted_result": "H"}

        sim = self.ko.simulate_knockout_from_table(ranked, rng, predict_fn)
        half_a_seeds = {m["seed_pos"] for m in sim["round_of_16"]["half_a"]}
        half_b_seeds = {m["seed_pos"] for m in sim["round_of_16"]["half_b"]}
        self.assertEqual(half_a_seeds, {1, 8, 4, 5})
        self.assertEqual(half_b_seeds, {2, 7, 3, 6})
        # Seeds 1 and 2 must be on opposite halves.
        self.assertIn(1, half_a_seeds)
        self.assertIn(2, half_b_seeds)
        # Champion must be a real team from the table.
        self.assertTrue(str(sim["champion"]).startswith("Seed"))
        self.assertNotEqual(sim["champion"], sim["runner_up"])

    def test_finish_odds_maps_for_website(self):
        ranked = [f"Team{i}" for i in range(1, 25)]
        rng = np.random.default_rng(1)
        counts = self.ko.empty_finish_counts(ranked)
        for _ in range(20):
            sim = self.ko.simulate_knockout_from_table(ranked, rng, None)
            self.ko.accumulate_finish_counts(counts, sim, ranked)
        probs = self.ko.finish_counts_to_probabilities(counts, 20)
        reach = self.ko.finish_probs_to_round_reach(probs)
        elim = self.ko.finish_probs_to_elimination(probs)
        self.assertIn("Round of 16", reach)
        self.assertIn("Winner", reach)
        self.assertIn("Playoff", reach)
        # Top-8 skip playoff: playoff reach for Team1 should be 0.
        self.assertEqual(probs["Team1"]["playoff"], 0.0)
        self.assertEqual(probs["Team1"]["round_of_16"], 100.0)
        # Seeds 9-24 always enter playoff in a full 24-team table.
        self.assertEqual(probs["Team12"]["playoff"], 100.0)
        self.assertIn("Team1", elim)
        self.assertIn("Champion", elim["Team1"])
        # Winner probs should sum ~100 across teams.
        total_w = sum(p["winner"] for p in probs.values())
        self.assertAlmostEqual(total_w, 100.0, delta=0.5)


class ProjectUEFACupsSmokeTests(unittest.TestCase):
    def test_module_imports_and_exports_refresh(self):
        mod = importlib.import_module("Project_UEFA_Cups")
        self.assertTrue(callable(mod.refresh_uefa_cup_projections))
        self.assertTrue(hasattr(mod, "project_competition"))

    def test_track_wires_uefa_engine(self):
        src = (FILES_DIR / "Track_Cup_Results.py").read_text(encoding="utf-8")
        self.assertIn("Project_UEFA_Cups", src)
        self.assertIn("refresh_uefa_cup_projections", src)
        self.assertIn("skip legacy UEFA brackets", src)


if __name__ == "__main__":
    unittest.main()
