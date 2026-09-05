#!/usr/bin/env python3
"""Guards for new-season projections after manual backend/DB restart."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class StartupFullRetrainTests(unittest.TestCase):
    def test_startup_requests_full_retrain_by_default(self):
        source = (ROOT / "Backend" / "server.py").read_text(encoding="utf-8")
        self.assertIn('PIPELINE_FULL_RETRAIN_ON_START", "1"', source)
        self.assertIn("full_retrain=full_on_start", source)
        self.assertNotIn(
            'self._run_pipeline_in_background(trigger="startup", full_retrain=False)',
            source,
        )


class PathBRosterGateTests(unittest.TestCase):
    def test_global_path_b_uses_roster_not_hardcoded_league_set(self):
        source = (ROOT / "files" / "Project_League_Table.py").read_text(encoding="utf-8")
        self.assertIn("if not _load_any_roster(competition):", source)
        self.assertNotIn(
            "if competition not in PRESEASON_FALLBACK_LEAGUES:",
            source,
        )

    def test_extra_path_b_uses_roster_not_hardcoded_league_set(self):
        source = (ROOT / "Extra-leagues" / "files" / "Project_League_Table.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("if not _load_any_roster(competition):", source)
        self.assertNotIn(
            "if competition not in PRESEASON_FALLBACK_LEAGUES:",
            source,
        )


class LeagueResultFreshnessTests(unittest.TestCase):
    def _load_helpers(self):
        # Parse only the helper functions to avoid importing sklearn-heavy predictions.py.
        path = ROOT / "Website" / "predictions.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wanted = {
            "_parse_league_result_generated_at",
            "_league_result_is_current_season",
        }
        ns: dict = {
            "datetime": datetime,
            "timezone": timezone,
            "os": os,
            "sys": sys,
            "__name__": "predictions_helpers",
        }
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
                code = compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec")
                exec(code, ns)
        return ns

    def test_rejects_june_finals_after_july_flip(self):
        helpers = self._load_helpers()
        payload = {
            "competition": "England/Premier League",
            "generated_at_utc": "2026-06-08T21:57:40+00:00",
        }
        import season_calendar as sc

        self.assertFalse(
            helpers["_league_result_is_current_season"](
                payload, "England/Premier League"
            )
        )
        payload["generated_at_utc"] = "2026-08-01T12:00:00+00:00"
        self.assertTrue(
            helpers["_league_result_is_current_season"](
                payload, "England/Premier League"
            )
        )
        self.assertEqual(sc.european_season_start_year(), 2026)


if __name__ == "__main__":
    unittest.main()
