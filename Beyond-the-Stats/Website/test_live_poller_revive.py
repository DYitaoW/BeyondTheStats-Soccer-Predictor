#!/usr/bin/env python3
"""Regression tests for live-poller revive + zeroed projection rebuild gates.

Avoid importing ``live_poller`` (it pulls sklearn-heavy prediction modules).
"""
from __future__ import annotations

import ast
import csv
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = Path(__file__).resolve().parent


def _load_config():
    spec = importlib.util.spec_from_file_location("bts_config", WEBSITE / "config.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _exec_named_functions(path: Path, names: set[str], ns: dict | None = None) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    ns = ns or {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            code = compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec")
            exec(code, ns)
    return ns


class LivePollerReviveTests(unittest.TestCase):
    def test_compute_poll_interval_signature_has_no_live_comps(self):
        """Interval helper must not require the undefined ``live_comps`` local.

        Production froze scores after the first cycle because the loop called
        ``_compute_poll_interval(..., live_comps, ...)`` while ``live_comps`` was
        never assigned in that scope, killing the daemon while ``started`` stayed
        true.
        """
        source = (WEBSITE / "live_poller.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_compute_poll_interval"
        )
        self.assertEqual([a.arg for a in fn.args.args], ["results", "todays_comps"])

        ns = _exec_named_functions(
            WEBSITE / "live_poller.py",
            {"_compute_poll_interval"},
            {"datetime": datetime, "ZoneInfo": ZoneInfo},
        )
        self.assertEqual(ns["_compute_poll_interval"]({}, {}), 1800)
        self.assertEqual(ns["_compute_poll_interval"](None, None), 1800)

    def test_poller_loop_does_not_load_undefined_live_comps(self):
        source = (WEBSITE / "live_poller.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        loop_fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_live_score_poller_loop"
        )
        loaded = {
            n.id
            for n in ast.walk(loop_fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        self.assertNotIn("live_comps", loaded)

    def test_status_and_start_support_dead_thread_revive(self):
        source = (WEBSITE / "live_poller.py").read_text(encoding="utf-8")
        self.assertIn('"thread_alive"', source)
        self.assertIn("force: bool = False", source)
        self.assertIn("start_live_score_poller(force=True)", source)


class ZeroedProjectionGateTests(unittest.TestCase):
    def test_mostly_zeroed_helper_forces_rebuild(self):
        ns = _exec_named_functions(
            ROOT / "Run_All_Pipeline.py",
            {"_projected_tables_are_mostly_zeroed"},
            {"os": os},
        )
        helper = ns["_projected_tables_are_mostly_zeroed"]
        self.assertTrue(helper("/no/such/file.csv"))
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "projected_league_tables.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["competition", "team", "sim_runs"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"competition": "England/Premier League", "team": "Arsenal", "sim_runs": "0"},
                        {"competition": "England/Premier League", "team": "Chelsea", "sim_runs": "0"},
                        {"competition": "Spain/La Liga", "team": "Barcelona", "sim_runs": "0"},
                    ]
                )
            self.assertTrue(helper(str(csv_path)))
            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["competition", "team", "sim_runs"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"competition": "England/Premier League", "team": "Arsenal", "sim_runs": "500"},
                        {"competition": "England/Premier League", "team": "Chelsea", "sim_runs": "500"},
                        {"competition": "Spain/La Liga", "team": "Barcelona", "sim_runs": "500"},
                    ]
                )
            self.assertFalse(helper(str(csv_path)))


class LiveScoreCoverageTests(unittest.TestCase):
    def test_portugal_and_eredivisie_remain_in_live_score_set(self):
        """Do not drop Liga Portugal / Eredivisie — fix the poller crash instead."""
        config = _load_config()
        self.assertIn("Portugal/Liga Portugal", config.LIVE_SCORE_COMPETITIONS)
        self.assertIn("Netherlands/Eredivisie", config.LIVE_SCORE_COMPETITIONS)
        self.assertNotIn("Portugal/Liga Portugal", config.RESULT_ONLY_COMPETITIONS)
        self.assertNotIn("Netherlands/Eredivisie", config.RESULT_ONLY_COMPETITIONS)


class RosterBomEncodingTests(unittest.TestCase):
    def test_project_league_table_reads_rosters_with_utf8_sig(self):
        for rel in (
            "files/Project_League_Table.py",
            "Extra-leagues/files/Project_League_Table.py",
        ):
            source = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn('encoding="utf-8-sig"', source)
            self.assertNotIn('encoding="utf-8"', source)


if __name__ == "__main__":
    unittest.main()
