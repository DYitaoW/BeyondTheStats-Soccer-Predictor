"""NWSL playoff projection, table-cache freshness, and test isolation of cache files.

- The NWSL playoffs (top 8, single elimination, 1v8 / 4v5 / 2v7 / 3v6, higher
  seed hosts) are seeded from the regular-season table; real playoff results
  are kept and only the remaining ties are simulated.
- A cached table stamped with a tz-aware ``updated_at`` used to be treated as
  fresh forever (naive ``now() - aware`` raised -> "fresh").
- Tests used to persist tables computed from their throwaway store into the
  production ``standings_cache.json`` / LeagueData files (live WSL/NWSL tables
  showed one game per club).
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website", ROOT / "shared"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_spec = importlib.util.spec_from_file_location(
    "womens_Nwsl_Playoffs", ROOT / "pipelines" / "womens" / "files" / "Nwsl_Playoffs.py")
nwsl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nwsl)

TEAMS = [f"T{i}" for i in range(1, 11)]  # T1 best ... T10 worst


def _standings():
    return {t: {"Pts": 60 - 3 * i, "W": 18 - i, "GD": 10 - i, "GF": 40 - i} for i, t in enumerate(TEAMS)}


def _stronger_home_wins(home, away):
    # Lower number = stronger; strong favourite wins 80%.
    return (0.7, 0.2, 0.1) if int(home[1:]) < int(away[1:]) else (0.1, 0.2, 0.7)


def _row(home, away, hg, ag, winner="", pens=("", "")):
    res = "H" if hg > ag else ("A" if ag > hg else "D")
    return {"Stage": "playoffs", "Home": home, "Away": away, "HG": str(hg), "AG": str(ag), "Res": res,
            "Winner": winner or (res if res != "D" else ""), "HPens": pens[0], "APens": pens[1],
            "Date": "01/11/2026", "MatchId": f"{home}-{away}"}


class SeedingAndBracketTests(unittest.TestCase):
    def test_rank_table_tiebreak_points_wins_gd_gf(self):
        st = {"A": {"Pts": 47, "W": 14, "GD": 4, "GF": 37}, "B": {"Pts": 47, "W": 14, "GD": 5, "GF": 44},
              "C": {"Pts": 47, "W": 15, "GD": 0, "GF": 10}}
        self.assertEqual(nwsl.rank_table(st), ["C", "B", "A"])

    def test_bracket_topology_and_higher_seed_hosts(self):
        out = nwsl.project_playoffs([nwsl.rank_table(_standings())], _stronger_home_wins, [], sim_runs=200)
        qf = [(g["home_seed"], g["away_seed"]) for g in out["quarterfinals"]]
        self.assertEqual(qf, [(1, 8), (4, 5), (2, 7), (3, 6)])
        self.assertEqual([(g["home_seed"], g["away_seed"]) for g in out["semifinals"]], [(1, 4), (2, 3)])
        self.assertEqual((out["final"]["home_seed"], out["final"]["away_seed"]), (1, 2))
        self.assertEqual(out["status"], "projected")
        self.assertNotIn("T9", out["make_playoffs_probabilities"])
        self.assertAlmostEqual(sum(out["championship_winner_probabilities"].values()), 100.0, delta=0.5)
        self.assertGreater(out["championship_winner_probabilities"]["T1"], out["championship_winner_probabilities"].get("T8", 0))

    def test_real_results_kept_including_penalty_winner_and_only_rest_simulated(self):
        raw = [
            _row("T1", "T8", 1, 0), _row("T4", "T5", 1, 1, winner="A", pens=("3", "4")),  # T5 on pens
            _row("T2", "T7", 0, 1), _row("T3", "T6", 2, 0),
            _row("T1", "T5", 0, 2), _row("T3", "T7", 3, 1),
            {"Stage": "playoffs", "Home": "T3", "Away": "T5", "Res": "", "Date": "22/11/2026",
             "Time": "20:00", "KickoffUTC": "2026-11-23T01:00:00Z", "MatchId": "final-1"},
        ]
        out = nwsl.project_playoffs([nwsl.rank_table(_standings())], _stronger_home_wins, raw, sim_runs=1000)
        self.assertEqual(out["status"], "in_progress")
        winners = [g["winner"] for g in out["quarterfinals"] + out["semifinals"]]
        self.assertEqual(winners, ["T1", "T5", "T7", "T3", "T5", "T3"])
        self.assertEqual(out["quarterfinals"][1]["score"], "1-1 (3-4 pens)")
        final = out["final"]
        self.assertEqual((final["home_team"], final["away_team"], final["played"]), ("T3", "T5", False))
        self.assertTrue(final.get("scheduled"))
        self.assertEqual(final["match_id"], "final-1")
        odds = out["championship_winner_probabilities"]
        self.assertEqual(set(odds), {"T3", "T5"})  # nobody else can win any more
        self.assertAlmostEqual(odds["T3"], 87.5, delta=2.5)  # 0.7 + 0.2 * (0.7 / 0.8)
        self.assertEqual(out["round_reach_probabilities"]["final"], {"T3": 100.0, "T5": 100.0})

    def test_complete_playoffs_report_champion(self):
        raw = [_row("T1", "T8", 1, 0), _row("T4", "T5", 2, 0), _row("T2", "T7", 1, 0), _row("T3", "T6", 1, 0),
               _row("T1", "T4", 1, 0), _row("T2", "T3", 0, 1), _row("T1", "T3", 0, 0, winner="A", pens=("2", "4"))]
        out = nwsl.project_playoffs([nwsl.rank_table(_standings())], _stronger_home_wins, raw, sim_runs=50)
        self.assertEqual((out["status"], out["champion"]), ("complete", "T3"))
        self.assertEqual(out["championship_winner_probabilities"], {"T3": 100.0})

    def test_mid_season_seeds_vary_with_simulated_finishes(self):
        a = nwsl.rank_table(_standings())
        b = list(a)
        b[7], b[8] = b[8], b[7]  # T9 sneaks into 8th in half the runs
        out = nwsl.project_playoffs([a, b], _stronger_home_wins, [], sim_runs=1000)
        self.assertAlmostEqual(out["make_playoffs_probabilities"]["T9"], 50.0, delta=0.5)
        self.assertAlmostEqual(out["make_playoffs_probabilities"]["T1"], 100.0)

    def test_advance_probability_resolves_draws_by_strength(self):
        self.assertAlmostEqual(nwsl.advance_probability(lambda h, a: (0.5, 0.2, 0.3), "A", "B"), 0.5 + 0.2 * 0.625)
        self.assertEqual(nwsl.advance_probability(lambda h, a: (0, 0, 0), "A", "B"), 0.5)


class LeagueDataBracketTests(unittest.TestCase):
    def test_bracket_converted_to_cup_style_knockout(self):
        import league_data as ld

        raw = [_row("T1", "T8", 1, 0), _row("T4", "T5", 2, 0), _row("T2", "T7", 1, 0), _row("T3", "T6", 1, 0)]
        projected = nwsl.project_playoffs([nwsl.rank_table(_standings())], _stronger_home_wins, raw, sim_runs=100)
        ko = ld._nwsl_bracket_to_cup_knockout(projected)
        self.assertEqual(ko["stages"], ["quarterfinals", "semifinals", "final"])
        qf1 = ko["knockout"]["quarterfinals"][0]
        self.assertEqual((qf1["status"], qf1["winner"], qf1["home_seed"]), ("FT", "T1", 1))
        sf = ko["knockout"]["semifinals"][0]
        self.assertEqual(sf["status"], "Projected")
        self.assertIsNotNone(sf["prob_home"])
        self.assertIsNone(ko["real_knockout"]["semifinals"][0]["winner"])  # not played yet
        self.assertEqual(ko["real_knockout"]["quarterfinals"][0]["winner"], "T1")


class TableFreshnessTests(unittest.TestCase):
    def test_aware_naive_and_unparseable_timestamps(self):
        from standings import table_age_seconds

        aware = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        naive = (datetime.now() - timedelta(minutes=2)).isoformat()
        self.assertAlmostEqual(table_age_seconds(aware), 3 * 3600, delta=5)
        self.assertAlmostEqual(table_age_seconds(naive), 120, delta=5)
        self.assertAlmostEqual(table_age_seconds(aware.replace("+00:00", "Z")), 3 * 3600, delta=5)
        self.assertIsNone(table_age_seconds("not a date"))

    def test_stale_aware_cached_table_is_not_fresh(self):
        import league_data as ld

        old = {"groups": [{"entries": []}], "updated_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()}
        self.assertFalse(ld._standings_entry_fresh(old))
        fresh = {"groups": [{"entries": []}], "updated_at": datetime.now(timezone.utc).isoformat()}
        self.assertTrue(ld._standings_entry_fresh(fresh))
        self.assertFalse(ld._standings_entry_fresh({"groups": [{}], "updated_at": "garbage"}))


class CacheFileIsolationTests(unittest.TestCase):
    def test_derived_caches_point_at_temp_dir_under_tests(self):
        import config
        from shared import paths

        self.assertIsNotNone(paths.TEST_OUTPUT_DIR)
        prod_status = paths.OUTPUT_STATUS_DIR.resolve()
        for value in (config.REAL_TABLES_PERSIST_FILE, config.LEAGUE_DATA_DIR, config.CUP_DATA_DIR,
                      config.POINTS_ADJUSTMENTS_FILE, paths.LEAGUE_LEADERS_CACHE_FILE, paths.OUTPUT_CACHE_DIR):
            resolved = Path(value).resolve()
            self.assertTrue(str(resolved).startswith(str(paths.TEST_OUTPUT_DIR.resolve())), value)
            self.assertFalse(str(resolved).startswith(str(prod_status)), value)
        from routes import leagues
        self.assertTrue(leagues._LEAGUE_LEADERS_CACHE_FILE.startswith(str(paths.TEST_OUTPUT_DIR)))


if __name__ == "__main__":
    unittest.main()
