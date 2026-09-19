#!/usr/bin/env python3
"""Extra pipeline must survive empty Extra Processed_Data via shared Europe fallback."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRA_FILES = ROOT / "pipelines" / "extra" / "files"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_predict_match():
    spec = importlib.util.spec_from_file_location(
        "extra_predict_match_under_test",
        EXTRA_FILES / "Predict_Match.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_project_league_table():
    # Project_League_Table imports Predict_Match as pm from the same files dir.
    files_dir = str(EXTRA_FILES)
    if files_dir not in sys.path:
        sys.path.insert(0, files_dir)
    spec = importlib.util.spec_from_file_location(
        "extra_project_league_table_under_test",
        EXTRA_FILES / "Project_League_Table.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class ExtraProcessedFallbackTests(unittest.TestCase):
    def test_resolve_processed_dir_falls_back_to_shared(self):
        pm = _load_predict_match()
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "extra_pd"
            shared = Path(tmp) / "shared_pd"
            empty.mkdir()
            season = shared / "Argentina" / "Primera Division"
            season.mkdir(parents=True)
            (season / "argstat2026.csv").write_text(
                "HomeTeam,AwayTeam,FTR,FTHG,FTAG,HS,AS,HST,AST\n"
                "A,B,H,1,0,5,3,2,1\n",
                encoding="utf-8",
            )
            pm.PROCESSED_DIR = str(empty)
            pm.SHARED_PROCESSED_DIR = str(shared)
            self.assertFalse(pm.processed_dir_has_season_csvs(str(empty)))
            self.assertTrue(pm.processed_dir_has_season_csvs(str(shared)))
            self.assertEqual(pm.resolve_processed_dir(), str(shared))

    def test_resolve_processed_dir_prefers_extra_when_present(self):
        pm = _load_predict_match()
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "extra_pd"
            shared = Path(tmp) / "shared_pd"
            for root, name in ((extra, "argstat2026.csv"), (shared, "premstat2026.csv")):
                season = root / "League"
                season.mkdir(parents=True)
                (season / name).write_text(
                    "HomeTeam,AwayTeam,FTR,FTHG,FTAG,HS,AS,HST,AST\n"
                    "A,B,H,1,0,5,3,2,1\n",
                    encoding="utf-8",
                )
            pm.PROCESSED_DIR = str(extra)
            pm.SHARED_PROCESSED_DIR = str(shared)
            self.assertEqual(pm.resolve_processed_dir(), str(extra))

    def test_load_training_matches_uses_shared_when_extra_empty(self):
        pm = _load_predict_match()
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "extra_pd"
            shared = Path(tmp) / "shared_pd"
            empty.mkdir()
            season = shared / "Japan" / "J1 League"
            season.mkdir(parents=True)
            (season / "jpnstat2026.csv").write_text(
                "HomeTeam,AwayTeam,FTR,FTHG,FTAG,HS,AS,HST,AST,"
                "AvgH,AvgD,AvgA,HomePointsBefore,AwayPointsBefore,"
                "HomeLeaguePosBefore,AwayLeaguePosBefore\n"
                "A,B,H,2,1,8,4,3,2,2.1,3.2,3.4,3,0,1,2\n",
                encoding="utf-8",
            )
            pm.PROCESSED_DIR = str(empty)
            pm.SHARED_PROCESSED_DIR = str(shared)
            matches, season_files = pm.load_training_matches()
            self.assertEqual(len(matches), 1)
            self.assertEqual(season_files, ["Japan/J1 League/jpnstat2026.csv"])

    def test_project_merges_global_raw_for_extra_competitions(self):
        plt = _load_project_league_table()
        self.assertIn("Netherlands/Eredivisie", plt.EXTRA_COMPETITIONS)
        self.assertTrue(hasattr(plt, "GLOBAL_RAW_DIR"))
        source = (EXTRA_FILES / "Project_League_Table.py").read_text(encoding="utf-8")
        self.assertIn("GLOBAL_RAW_DIR", source)
        self.assertIn("for raw_root in (RAW_DIR, GLOBAL_RAW_DIR)", source)

    def test_empty_extra_still_loads_via_copied_shared_tree(self):
        """Integration: hide Extra Processed_Data; shared copy must satisfy load_training_matches."""
        pm = _load_predict_match()
        real_extra = Path(pm.PROCESSED_DIR)
        if not pm.processed_dir_has_season_csvs(str(real_extra)):
            self.skipTest("Extra Processed_Data not populated in this environment")
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared_pd"
            # Copy a single competition tree into a fake shared Processed_Data.
            src_comp = next(real_extra.rglob("*stat*.csv")).parent
            rel = src_comp.relative_to(real_extra)
            dst = shared / rel
            dst.mkdir(parents=True)
            for csv_path in src_comp.glob("*.csv"):
                shutil.copy2(csv_path, dst / csv_path.name)
            empty = Path(tmp) / "empty_extra"
            empty.mkdir()
            pm.PROCESSED_DIR = str(empty)
            pm.SHARED_PROCESSED_DIR = str(shared)
            matches, season_files = pm.load_training_matches()
            self.assertGreater(len(matches), 0)
            self.assertTrue(season_files)


if __name__ == "__main__":
    unittest.main()
