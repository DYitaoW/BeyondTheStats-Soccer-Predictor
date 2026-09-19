#!/usr/bin/env python3
"""Guard post-reorg path wiring: generated artifacts must live under Output/."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PostReorgPathConstantsTests(unittest.TestCase):
    def setUp(self):
        # Reload so a prior migration monkeypatch cannot leak into these asserts.
        import importlib
        import shared.paths as paths_mod
        import Website.config as config_mod

        importlib.reload(paths_mod)
        importlib.reload(config_mod)
        self.paths = paths_mod
        self.config = config_mod

    def test_status_files_under_output_status(self):
        status = self.paths.OUTPUT_STATUS_DIR
        path_attrs = (
            "LAST_REFRESH_FILE",
            "BACKEND_RUN_STATUS_FILE",
            "PIPELINE_STATUS_FILE",
            "LIVE_SCORE_HISTORY_FILE",
        )
        for attr in path_attrs:
            path = getattr(self.paths, attr)
            self.assertTrue(
                str(path).startswith(str(status)),
                f"{attr}={path} not under {status}",
            )
            self.assertEqual(str(getattr(self.config, attr)), str(path))
        # Website exposes standings cache as REAL_TABLES_PERSIST_FILE.
        self.assertEqual(
            self.config.REAL_TABLES_PERSIST_FILE,
            str(self.paths.STANDINGS_CACHE_FILE),
        )
        self.assertTrue(
            str(self.paths.STANDINGS_CACHE_FILE).startswith(str(status))
        )

    def test_prediction_csvs_under_output_predictions(self):
        pred_root = self.paths.OUTPUT_PREDICTIONS_DIR
        mapping = {
            "GLOBAL_UPCOMING_FILE": self.paths.OUTPUT_PRED_EUROPE,
            "MLS_UPCOMING_FILE": self.paths.OUTPUT_PRED_MLS,
            "EXTRA_UPCOMING_FILE": self.paths.OUTPUT_PRED_EXTRA,
            "CUP_UPCOMING_FILE": self.paths.OUTPUT_PRED_CUPS,
            "WORLD_CUP_PROJECTION_FILE": self.paths.OUTPUT_PRED_NATIONAL,
            "GLOBAL_PROJECTED_TABLE_FILE": self.paths.OUTPUT_PRED_EUROPE,
            "MLS_PROJECTED_TABLE_FILE": self.paths.OUTPUT_PRED_MLS,
            "EXTRA_PROJECTED_TABLE_FILE": self.paths.OUTPUT_PRED_EXTRA,
            "PAST_GAMES_FILE": self.paths.OUTPUT_PRED_SHARED,
        }
        for attr, region_dir in mapping.items():
            path = getattr(self.paths, attr)
            self.assertTrue(str(path).startswith(str(pred_root)), f"{attr} not under Predictions")
            self.assertTrue(str(path).startswith(str(region_dir)), f"{attr} wrong region dir")
            self.assertEqual(str(getattr(self.config, attr)), str(path))

    def test_europe_processed_is_project_data_not_files_dir(self):
        self.assertEqual(
            self.config.EUROPE_PROCESSED_DIR,
            str(self.paths.DATA_DIR / "Processed_Data"),
        )
        self.assertNotEqual(
            self.config.EUROPE_PROCESSED_DIR,
            os.path.join(self.config.FILES_DIR, "Processed_Data"),
        )

    def test_daily_pipeline_uses_output_status_paths(self):
        source = (ROOT / "main" / "Daily_Pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn('SP_DIR / "Data" / "standings_cache.json"', source)
        self.assertNotIn('SP_DIR / "Data" / "live_score_history.json"', source)
        self.assertNotIn('SP_DIR / "Data" / "backend_run_status.json"', source)
        self.assertIn("_paths.STANDINGS_CACHE_FILE", source)
        self.assertIn("_paths.LIVE_SCORE_HISTORY_FILE", source)
        self.assertIn("_paths.BACKEND_RUN_STATUS_FILE", source)

    def test_bts_status_uses_output_paths(self):
        source = (ROOT / "main" / "bts.py").read_text(encoding="utf-8")
        self.assertNotIn('Data" / "Predictions"', source)
        self.assertIn("_paths.GLOBAL_UPCOMING_FILE", source)
        self.assertIn("_paths.BACKEND_RUN_STATUS_FILE", source)

    def test_app_world_cup_uses_config_constant(self):
        source = (ROOT / "Website" / "app.py").read_text(encoding="utf-8-sig")
        self.assertIn("config.WORLD_CUP_PROJECTION_FILE", source)
        # Active (non-comment) hard-coded Data/Predictions world-cup path must be gone.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "join":
                continue
            flat = ast.dump(node)
            if "Predictions" in flat and "world_cup" in flat:
                self.fail(f"stale world_cup join still present: {flat[:200]}")

    def test_track_cup_does_not_overwrite_base_dir_to_region(self):
        source = (ROOT / "pipelines" / "europe" / "files" / "Track_Cup_Results.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
            source,
        )
        self.assertIn("ESPN_CUP_NAMES_FILE = str(_bts_paths.ESPN_CUP_NAMES_FILE)", source)

    def test_world_cup_pipeline_verifies_output_predictions_path(self):
        source = (ROOT / "pipelines" / "europe" / "files" / "Run_World_Cup_Pipeline.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("WORLD_CUP_PROJECTION_FILE", source)
        self.assertNotIn(
            'os.path.join(DATA_DIR, "world_cup_projection.json")',
            source,
        )
        self.assertNotIn(
            'os.path.join(DATA_DIR, "projected_cup_brackets.json")',
            source,
        )

    def test_europe_test_train_uses_sp_dir_base(self):
        source = (ROOT / "pipelines" / "europe" / "files" / "Test_train.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("project_root = BASE_DIR", source)
        self.assertNotIn(
            "project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
            source,
        )

    def test_predictions_form_cache_uses_europe_processed_dir(self):
        source = (ROOT / "Website" / "predictions.py").read_text(encoding="utf-8")
        self.assertIn('"global": config.EUROPE_PROCESSED_DIR', source)
        self.assertNotIn('os.path.join(config.FILES_DIR, "Processed_Data")', source)


class ExtraEmptyDataSoftFailTests(unittest.TestCase):
    def test_extra_sort_and_process_soft_skip_empty(self):
        sort_src = (ROOT / "pipelines" / "extra" / "files" / "Sort_Data.py").read_text(
            encoding="utf-8"
        )
        proc_src = (ROOT / "pipelines" / "extra" / "files" / "Process_Data.py").read_text(
            encoding="utf-8"
        )
        upcoming_src = (
            ROOT / "pipelines" / "extra" / "files" / "Predict_Upcoming_Matchweek.py"
        ).read_text(encoding="utf-8")
        self.assertIn("skipping Sort_Data", sort_src)
        self.assertIn("skipping Process_Data", proc_src)
        self.assertNotIn(
            'raise ValueError("No valid extra-league season files were found in Raw_Data.")',
            proc_src,
        )
        self.assertIn("falling back to ESPN-only", upcoming_src)
        self.assertNotIn(
            "No raw season files found for extra-league competitions",
            upcoming_src,
        )


class MlsProcessedFallbackPresenceTests(unittest.TestCase):
    def test_mls_predict_match_has_shared_fallback_helpers(self):
        source = (ROOT / "pipelines" / "mls" / "files" / "Predict_Match.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def resolve_processed_dir", source)
        self.assertIn("SHARED_PROCESSED_DIR", source)
        self.assertIn("falling back to shared", source)


class MlsUpcomingFramesFallbackTests(unittest.TestCase):
    def test_load_upcoming_does_not_concat_undefined_frames(self):
        source = (
            ROOT / "pipelines" / "mls" / "files" / "Predict_Upcoming_Matchweek.py"
        ).read_text(encoding="utf-8")
        self.assertIn("load_upcoming_matchweek_fixtures_from_csv_fallback(window_days)", source)
        # Dead code after empty ESPN used to call pd.concat(frames) / iterate sources
        # without defining them → UnboundLocalError on production.
        self.assertNotIn("pd.concat(frames, ignore_index=True)\n    fixtures = dedupe_fixtures", source)
        self.assertNotIn("for source in sources:", source)
        self.assertIn('print("Fixture source: CSV fallback")', source)


class UefaCupApiDeferralTests(unittest.TestCase):
    def test_early_finished_api_excludes_uefa_cups(self):
        source = (
            ROOT / "pipelines" / "europe" / "files" / "Predict_Upcoming_Matchweek.py"
        ).read_text(encoding="utf-8")
        self.assertIn("UEFA_CUP_API_COMPETITIONS", source)
        self.assertIn("DOMESTIC_CUP_API_COMPETITIONS", source)
        self.assertIn("def load_uefa_cup_finished_matches_from_api", source)
        self.assertIn(
            "competitions = {**API_COMPETITIONS, **DOMESTIC_CUP_API_COMPETITIONS}",
            source,
        )
        # Must not merge all cups into the early finished-match loop anymore.
        self.assertNotIn(
            "{**API_COMPETITIONS, **CUP_API_COMPETITIONS}",
            source,
        )

    def test_cups_last_loads_uefa_finished_via_api(self):
        source = (
            ROOT / "pipelines" / "europe" / "files" / "Predict_Upcoming_Cups.py"
        ).read_text(encoding="utf-8")
        self.assertIn("load_uefa_cup_finished_matches_from_api", source)
        self.assertIn("load_results_index(RAW_DATA_DIR, api_token=args.api_token)", source)


class EuropeDataDirAliasTests(unittest.TestCase):
    def test_europe_data_dir_is_project_data(self):
        self.assertEqual(self.paths.EUROPE_DATA_DIR, self.paths.DATA_DIR)
        self.assertEqual(str(self.config.EUROPE_DATA_DIR), str(self.paths.DATA_DIR))

    def setUp(self):
        import importlib
        import shared.paths as paths_mod
        import Website.config as config_mod

        importlib.reload(paths_mod)
        importlib.reload(config_mod)
        self.paths = paths_mod
        self.config = config_mod


class LegacyRuntimeMigrationTests(unittest.TestCase):
    def test_migrate_copies_legacy_backend_status(self):
        import importlib
        import tempfile
        from pathlib import Path
        import shared.paths as paths_mod

        attrs = (
            "DATA_DIR",
            "BACKEND_RUN_STATUS_FILE",
            "STANDINGS_CACHE_FILE",
            "LIVE_SCORE_HISTORY_FILE",
            "PIPELINE_STATUS_FILE",
            "LAST_REFRESH_FILE",
            "LAST_DATA_REFRESH_FILE",
            "PREDICTION_TRACKING_FILE",
            "WORLD_CUP_PROJECTION_FILE",
            "GLOBAL_UPCOMING_FILE",
            "GLOBAL_PROJECTED_TABLE_FILE",
            "PAST_GAMES_FILE",
        )
        saved = {name: getattr(paths_mod, name) for name in attrs}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                legacy = Path(tmp) / "Data" / "backend_run_status.json"
                dest = Path(tmp) / "Output" / "Status" / "backend_run_status.json"
                legacy.parent.mkdir(parents=True)
                legacy.write_text('{"ok": true}', encoding="utf-8")
                paths_mod.DATA_DIR = Path(tmp) / "Data"
                paths_mod.BACKEND_RUN_STATUS_FILE = dest
                paths_mod.STANDINGS_CACHE_FILE = Path(tmp) / "Output" / "Status" / "standings_cache.json"
                paths_mod.LIVE_SCORE_HISTORY_FILE = Path(tmp) / "Output" / "Status" / "live_score_history.json"
                paths_mod.PIPELINE_STATUS_FILE = Path(tmp) / "Output" / "Status" / "pipeline_status.json"
                paths_mod.LAST_REFRESH_FILE = Path(tmp) / "Output" / "Status" / "last_refresh.json"
                paths_mod.LAST_DATA_REFRESH_FILE = Path(tmp) / "Output" / "Status" / "last_data_refresh.json"
                paths_mod.PREDICTION_TRACKING_FILE = Path(tmp) / "Output" / "Status" / "prediction_tracking.json"
                paths_mod.WORLD_CUP_PROJECTION_FILE = (
                    Path(tmp) / "Output" / "Predictions" / "national" / "world_cup_projection.json"
                )
                paths_mod.GLOBAL_UPCOMING_FILE = (
                    Path(tmp) / "Output" / "Predictions" / "europe" / "upcoming_matchweek_predictions.csv"
                )
                paths_mod.GLOBAL_PROJECTED_TABLE_FILE = (
                    Path(tmp) / "Output" / "Predictions" / "europe" / "projected_league_tables.csv"
                )
                paths_mod.PAST_GAMES_FILE = (
                    Path(tmp) / "Output" / "Predictions" / "shared" / "past_games.json"
                )
                paths_mod.migrate_legacy_runtime_files()
                self.assertTrue(dest.is_file())
                self.assertEqual(dest.read_text(encoding="utf-8"), '{"ok": true}')
        finally:
            for name, value in saved.items():
                setattr(paths_mod, name, value)
            importlib.reload(paths_mod)

    def test_migrate_legacy_mls_raw_csvs_into_pipelines_tree(self):
        import importlib
        import tempfile
        from pathlib import Path
        import shared.paths as paths_mod

        attrs = ("SP_DIR", "MLS_DATA_DIR", "EXTRA_DATA_DIR", "EUROPE_DIR", "DATA_DIR")
        saved = {name: getattr(paths_mod, name) for name in attrs}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                legacy_mls = root / "MLS" / "Data" / "Raw_Data" / "United States" / "MLS"
                legacy_mls.mkdir(parents=True)
                (legacy_mls / "mlsstat2025.csv").write_text("Season,Date\n", encoding="utf-8")
                new_mls = root / "pipelines" / "mls" / "Data"
                paths_mod.SP_DIR = root
                paths_mod.MLS_DATA_DIR = new_mls
                paths_mod.EXTRA_DATA_DIR = root / "pipelines" / "extra" / "Data"
                paths_mod.EUROPE_DIR = root / "pipelines" / "europe"
                paths_mod.DATA_DIR = root / "Data"
                paths_mod.migrate_legacy_working_data()
                dest = new_mls / "Raw_Data" / "United States" / "MLS" / "mlsstat2025.csv"
                self.assertTrue(dest.is_file(), f"expected migrated CSV at {dest}")
                self.assertEqual(dest.read_text(encoding="utf-8"), "Season,Date\n")
        finally:
            for name, value in saved.items():
                setattr(paths_mod, name, value)
            importlib.reload(paths_mod)


if __name__ == "__main__":
    unittest.main()
