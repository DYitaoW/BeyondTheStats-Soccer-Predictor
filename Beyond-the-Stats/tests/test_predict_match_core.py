"""Single Predict_Match implementation (``pipelines/predict_match_core.py``).

The four regional ``Predict_Match.py`` files are profiles that set ``REGION``
and exec the core into their own namespace. These tests pin what callers rely
on: one isolated module per region, region paths / patterns / model settings,
and region-only names (``hasattr`` probes in the projection scripts).
Full old-vs-new parity (training data, features, trained models, interactive
``main()`` output) was verified when the copies were merged (2026-11-18).
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGIONS = ("europe", "mls", "extra", "womens")


def _load(region: str):
    files = ROOT / "pipelines" / region / "files"
    sys.path.insert(0, str(files))
    try:
        spec = importlib.util.spec_from_file_location(f"t_pm_{region}", files / "Predict_Match.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(files))


class PredictMatchCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mods = {r: _load(r) for r in REGIONS}

    def test_profiles_are_thin_and_share_one_core(self):
        for r in REGIONS:
            text = (ROOT / "pipelines" / r / "files" / "Predict_Match.py").read_text(encoding="utf-8")
            self.assertIn(f'REGION = "{r}"', text)
            self.assertLess(len(text.splitlines()), 40, r)
            self.assertEqual(self.mods[r].REGION, r)
            self.assertTrue(self.mods[r].__file__.endswith(f"{r}/files/Predict_Match.py"))

    def test_modules_are_isolated_with_region_paths(self):
        m = self.mods
        self.assertTrue(m["europe"].PROCESSED_DIR.endswith("Beyond-the-Stats/Data/Processed_Data"))
        for r in ("mls", "extra", "womens"):
            self.assertIn(f"pipelines/{r}/Data/Processed_Data", m[r].PROCESSED_DIR)
            self.assertIn(f"pipelines/{r}/Data/Team_Data/model_cache.pkl", m[r].MODEL_CACHE)
        self.assertEqual(len({id(v.load_training_matches) for v in m.values()}), 4)
        m["mls"]._name_mapping_cache = {"sentinel": 1}
        self.assertNotEqual(m["extra"]._name_mapping_cache, {"sentinel": 1})

    def test_region_season_patterns(self):
        m = self.mods
        self.assertEqual(m["europe"].parse_season_start_year("engstat2024-25.csv"), 2024)
        self.assertIsNone(m["europe"].parse_season_start_year("mlsstat2024.csv"))
        self.assertEqual(m["mls"].parse_season_start_year("mlsstat2024.csv"), 2024)
        self.assertIsNone(m["mls"].parse_season_start_year("engstat2024-25.csv"))
        for r in ("extra", "womens"):
            self.assertEqual(m[r].parse_season_start_year("argstat2024.csv"), 2024)
            self.assertEqual(m[r].parse_season_start_year("wslstat2024-25.csv"), 2024)
            self.assertIsNone(m[r].parse_season_start_year("wslstat2024-26.csv"))

    def test_region_only_names(self):
        m = self.mods
        self.assertTrue(hasattr(m["europe"], "build_reasoning_string"))
        self.assertFalse(hasattr(m["europe"], "AveragedProbaClassifier"))
        self.assertFalse(hasattr(m["europe"], "resolve_processed_dir"))
        self.assertFalse(hasattr(m["mls"], "clear_name_mapping_cache"))
        self.assertFalse(hasattr(m["mls"], "MLS_MARKET_POSITION_WEIGHTS"))
        for r in ("mls", "extra", "womens"):
            self.assertTrue(hasattr(m[r], "AveragedProbaClassifier"), r)
            self.assertTrue(hasattr(m[r], "RESULT_XGB_PARAMS"), r)
            self.assertFalse(hasattr(m[r], "EU_RANDOMIZER_MAX_DELTA"), r)
        for r in ("europe", "extra", "womens"):
            self.assertTrue(hasattr(m[r], "clear_name_mapping_cache"), r)

    def test_region_behaviour_switches(self):
        m = self.mods
        self.assertEqual(m["europe"].season_recency_coefficient(2026, 2025), 0.92)
        self.assertEqual(m["mls"].season_recency_coefficient(2026, 2025), 0.80)
        self.assertEqual(m["mls"].season_recency_coefficient(2026, 2010), 0.12)
        from datetime import datetime

        self.assertEqual(m["mls"].expected_current_latest_start_year(datetime(2026, 3, 1)), 2026)
        self.assertEqual(m["extra"].expected_current_latest_start_year(datetime(2026, 3, 1)), 2025)
        p = {"H": 0.3, "D": 0.45, "A": 0.25}
        self.assertNotEqual(m["europe"].reduce_draw_probability(dict(p)), m["mls"].reduce_draw_probability(dict(p)))
        self.assertEqual(m["extra"].reduce_draw_probability(dict(p)), m["womens"].reduce_draw_probability(dict(p)))
        mv = {"teams": {"A": {"squad_value_eur_m": 12.5, "top_5_players": [
            {"market_value_eur": 1_000_000, "position_group": "attacker"}]}}}
        self.assertEqual(m["mls"].market_value_team_score("A", mv), 12.5)
        self.assertGreater(m["extra"].market_value_team_score("A", mv), 1000)

    def test_core_cannot_be_imported_directly(self):
        spec = importlib.util.spec_from_file_location("t_core", ROOT / "pipelines" / "predict_match_core.py")
        mod = importlib.util.module_from_spec(spec)
        with self.assertRaises(ImportError):
            spec.loader.exec_module(mod)


if __name__ == "__main__":
    unittest.main()
