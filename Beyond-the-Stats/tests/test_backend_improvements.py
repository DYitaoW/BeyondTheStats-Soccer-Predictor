"""Regression tests for the 2026-11-17 backend review fixes.

- HTML pages 500'd (Starlette 1.x ``TemplateResponse`` order, Flask ``url_for``)
- probability text 100x too big ("1353.0" for 13.53%)
- stale played games in the upcoming feed; ``window=4week`` overwritten
- postponed / cancelled games archived as 0-0 results (all writers)
- random "randomizer" noise / random home advantage / random national pick
- 0% probabilities; per-league draw calibration
- vectorized Monte Carlo parity (incl. head-to-head tiebreaks)
- asian handicap removed; slim /api/upcoming/global rows
- canonical_team_name memo; stale-while-revalidate response cache
- per-competition live polling; probabilistic accuracy metrics

Dates are relative to wall-clock time; nothing touches ESPN or the real store.
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import random
import sqlite3
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website", ROOT / "shared"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared import espn_status, monte_carlo, probability_calibration as pc  # noqa: E402


def _et_day(offset: int) -> str:
    return (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=offset)).strftime("%Y-%m-%d")


class PagesRenderTests(unittest.TestCase):
    def test_html_pages_render_with_static_urls(self):
        from starlette.testclient import TestClient
        from app import app

        client = TestClient(app)
        for path in ("/", "/leagues", "/about"):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn("/graphics/BeyondTheStatsLogo.png", resp.text)

    def test_flask_style_url_for(self):
        from routes.compat import _flask_style_url_for

        self.assertEqual(_flask_style_url_for("static", filename="styles.css", v="1"), "/static/styles.css?v=1")
        self.assertEqual(_flask_style_url_for("serve_graphic", filename="a b.png"), "/graphics/a%20b.png")


class ProbabilityTextTests(unittest.TestCase):
    def test_percent_and_decimal_scales(self):
        from predictions import _enrich_json_past_row

        row = {"home_team": "A", "away_team": "B", "competition": "X", "match_date": _et_day(1),
               "prob_home": 13.53, "prob_draw": 24.86, "prob_away": 61.61}
        _enrich_json_past_row(row)
        self.assertEqual((row["prob_home_text"], row["prob_away_text"]), ("13.5", "61.6"))
        row = {"home_team": "A", "away_team": "B", "competition": "X", "match_date": _et_day(1),
               "prob_home": 0.1353, "prob_draw": 0.2486, "prob_away": 0.6161}
        _enrich_json_past_row(row)
        self.assertEqual(row["prob_home_text"], "13.5")


class StaleUpcomingTests(unittest.TestCase):
    def test_prune_stale_upcoming_games(self):
        from shared import sqlite_store

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "s.db"
            old, new = _et_day(-60), _et_day(2)
            sqlite_store.upsert_upcoming_games([
                {"home_team": "Old FC", "away_team": "Gone FC", "competition": "England/Premier League",
                 "match_date": old, "match_date_iso": old, "prediction_key": "old"},
                {"home_team": "New FC", "away_team": "Next FC", "competition": "England/Premier League",
                 "match_date": new, "match_date_iso": new, "prediction_key": "new"},
            ], source="global", db_path=db)
            self.assertEqual(sqlite_store.prune_stale_upcoming_games(db_path=db), 1)
            left = [r["home_team"] for r in sqlite_store.load_upcoming_games(db_path=db)]
            self.assertEqual(left, ["New FC"])

    def test_superseded_rows_pruned_but_unmatched_kept(self):
        import predictions as preds
        from shared import sqlite_store

        d2, d3 = _et_day(2), _et_day(3)
        comp = "Belgium/First Division A"

        def row(h, a, d, key, sched="0"):
            return {"home_team": h, "away_team": a, "competition": comp, "match_date": d, "match_date_iso": d,
                    "prediction_key": key, "schedule_only": sched, "has_prediction": sched == "0"}

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "p.db"
            old = [row("Racing Club", "Lommel SK", d2, None, "1"),      # old wrong name, schedule only
                   row("Some FC", "Other FC", d3, "keep-me", "0")]       # not in this sync: kept
            sqlite_store.upsert_upcoming_games(old, source="extra", db_path=db)
            time.sleep(1.1)
            since = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            new = [row("Genk", "Lommel SK", d2, "genk-lommel")]
            sqlite_store.upsert_upcoming_games(new, source="extra", db_path=db)

            class Store:
                def __getattr__(self, name):
                    fn = getattr(sqlite_store, name)
                    return lambda *a, **k: fn(*a, db_path=db, **k)

            self.assertEqual(preds._prune_superseded_upcoming_rows(Store(), "extra", since, new), 1)
            left = sorted(r["home_team"] for r in sqlite_store.load_upcoming_games(db_path=db))
            self.assertEqual(left, ["Genk", "Some FC"])

    def test_mode_rows_default_to_today(self):
        import routes.predictions as rp
        from shared import sqlite_store

        with mock.patch.object(rp, "_load_upcoming_rows", return_value=([], {}, [])), \
                mock.patch.object(sqlite_store, "load_upcoming_games", return_value=[]) as load, \
                mock.patch.object(rp, "_annotate_upcoming_rows_with_live", side_effect=lambda r: r), \
                mock.patch.object(rp, "_deduplicate_and_merge_upcoming_rows", side_effect=lambda r: r):
            rp.load_upcoming_mode_rows("extra")
        self.assertEqual(load.call_args.kwargs["from_date"], _et_day(0))


class NotPlayedStatusTests(unittest.TestCase):
    def test_status_helpers(self):
        cancelled = {"name": "STATUS_CANCELED", "state": "post", "completed": False}
        postponed = {"name": "STATUS_POSTPONED", "state": "post", "completed": False}
        final = {"name": "STATUS_FULL_TIME", "state": "post", "completed": True}
        self.assertTrue(espn_status.is_not_played_status(cancelled))
        self.assertFalse(espn_status.is_played_final(cancelled))
        self.assertFalse(espn_status.is_played_final(postponed))
        self.assertTrue(espn_status.is_played_final(final))
        event = {"competitions": [{"status": {"type": cancelled}}]}
        self.assertEqual(espn_status.status_type_of(event)["name"], "STATUS_CANCELED")

    def test_friendlies_and_national_parsers_skip_cancelled(self):
        files = ROOT / "pipelines" / "europe" / "files"
        sys.path.insert(0, str(files))
        try:
            mods = {}
            for name in ("Update_Club_Friendlies", "Process_National_Team_Data"):
                spec = importlib.util.spec_from_file_location(f"t_{name}", files / f"{name}.py")
                mods[name] = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mods[name])
        finally:
            sys.path.remove(str(files))
        when = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")
        ev = {"id": "1", "date": when, "competitions": [{
            "status": {"type": {"name": "STATUS_CANCELED", "state": "post", "completed": False}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"displayName": "Jordan"}},
                            {"homeAway": "away", "score": "0", "team": {"displayName": "Indonesia"}}]}]}
        self.assertIsNone(mods["Update_Club_Friendlies"].parse_event(ev))
        self.assertIsNone(mods["Process_National_Team_Data"].parse_espn_event(ev, "International/Friendly"))


class CalibrationTests(unittest.TestCase):
    def test_floor(self):
        out = pc.finalize_probabilities({"H": 0.97774, "D": 0.02226, "A": 0.0})
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)
        self.assertGreaterEqual(min(out.values()), pc.PROB_FLOOR - 1e-12)
        self.assertGreater(out["H"], out["D"])

    def test_draw_calibration_hits_league_rate_and_keeps_ratio(self):
        import pandas as pd

        rng = random.Random(3)
        rows = []
        for i in range(40):
            h = rng.uniform(0.25, 0.55)
            d = 0.32  # scale 0.25 / 0.32 = 0.78, inside the clamp
            rows.append({"competition": "England/Premier League", "prob_home": h, "prob_draw": d,
                         "prob_away": 1 - h - d, "predicted_result": "D",
                         "odds_home": 0, "odds_draw": 0, "odds_away": 0})
        rows.append({"competition": "England/Premier League", "prob_home": 0, "prob_draw": 0, "prob_away": 0,
                     "predicted_result": ""})  # schedule-only: untouched
        df = pc.calibrate_prediction_frame(pd.DataFrame(rows), rates={"England/Premier League": 0.25})
        real = df.iloc[:40]
        self.assertAlmostEqual(real["prob_draw"].mean(), 0.25, places=3)
        r0 = rows[0]
        self.assertAlmostEqual(real.iloc[0]["prob_home"] / real.iloc[0]["prob_away"], r0["prob_home"] / r0["prob_away"], places=4)
        self.assertNotIn("D", set(real["predicted_result"]))
        self.assertAlmostEqual(real.iloc[0]["odds_home"], round(1 / real.iloc[0]["prob_home"], 2))
        self.assertEqual(df.iloc[40]["prob_home"], 0)

    def test_duplicate_index_rows_all_floored(self):
        import pandas as pd

        a = pd.DataFrame([{"competition": "X", "prob_home": 0.73, "prob_draw": 0.27, "prob_away": 0.0,
                           "predicted_result": "D"}] * 2)
        b = a.copy()
        b["prob_away"] = 0.001
        out = pc.calibrate_prediction_frame(pd.concat([a, b]))  # index 0, 1, 0, 1
        self.assertEqual(list(out.index), [0, 1, 0, 1])
        self.assertTrue((out["prob_away"] >= pc.PROB_FLOOR - 1e-9).all())
        self.assertEqual(set(out["predicted_result"]), {"H"})

    def test_scale_is_clamped(self):
        import pandas as pd

        rows = [{"competition": "X", "prob_home": 0.45, "prob_draw": 0.10, "prob_away": 0.45} for _ in range(10)]
        df = pc.calibrate_prediction_frame(pd.DataFrame(rows), rates={"X": 0.30})
        self.assertAlmostEqual(df["prob_draw"].mean(), 0.10 * pc.DRAW_SCALE_MAX, places=3)


class NoNoiseTests(unittest.TestCase):
    def test_randomizer_and_home_advantage_are_deterministic(self):
        files = ROOT / "pipelines" / "mls" / "files"
        sys.path.insert(0, str(files))
        try:
            spec = importlib.util.spec_from_file_location("t_mls_pm", files / "Predict_Match.py")
            pm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pm)
        finally:
            sys.path.remove(str(files))
        p = {"H": 0.5, "D": 0.3, "A": 0.2}
        outs = {tuple(round(v, 9) for v in pm.apply_probability_randomizer(p, 0.16, seed=s).values()) for s in range(20)}
        self.assertEqual(len(outs), 1)
        self.assertAlmostEqual(pm.apply_probability_randomizer(p, 0.16)["H"], 0.5)
        season = {"S": {"Home FC": {"avg_home_goals_scored": 2.0, "avg_home_goals_conceded": 1.0}}}
        shifts = {pm.mls_home_advantage_shift("Home FC", "S", season) for _ in range(20)}
        self.assertEqual(len(shifts), 1)


def _ref_positions(teams, base, fixtures, real, runs, h2h, seed):
    """Old per-run Python simulation (reference for parity)."""
    rng = random.Random(seed)
    pos = defaultdict(lambda: defaultdict(int))
    for _ in range(runs):
        tab = {t: dict(v) for t, v in base.items()}
        matches = list(real)
        for f in fixtures:
            p = f["probs"]
            res = rng.choices("HDA", weights=[p["H"], p["D"], p["A"]])[0]
            hg, ag = monte_carlo.coerce_scoreline(res, f["pred_home_goals"], f["pred_away_goals"])
            h, a = tab[f["home_team"]], tab[f["away_team"]]
            for s, gf, ga in ((h, hg, ag), (a, ag, hg)):
                s["P"] += 1; s["GF"] += gf; s["GA"] += ga; s["GD"] = s["GF"] - s["GA"]
            if hg > ag:
                h["Pts"] += 3
            elif ag > hg:
                a["Pts"] += 3
            else:
                h["Pts"] += 1; a["Pts"] += 1
            matches.append((f["home_team"], f["away_team"], hg, ag))
        if not h2h:
            ranked = sorted(tab, key=lambda t: (-tab[t]["Pts"], -tab[t]["GD"], -tab[t]["GF"], t))
        else:
            groups = defaultdict(list)
            for t in tab:
                groups[tab[t]["Pts"]].append(t)
            ranked = []
            for pts in sorted(groups, reverse=True):
                tied = groups[pts]
                sc = {t: [0, 0, 0] for t in tied}
                for hh, aa, hg, ag in matches:
                    if hh in sc and aa in sc:
                        sc[hh][2] += hg; sc[aa][2] += ag; sc[hh][1] += hg - ag; sc[aa][1] += ag - hg
                        if hg > ag: sc[hh][0] += 3
                        elif ag > hg: sc[aa][0] += 3
                        else: sc[hh][0] += 1; sc[aa][0] += 1
                ranked += sorted(tied, key=lambda t: (-sc[t][0], -sc[t][1], -sc[t][2], -tab[t]["GD"], -tab[t]["GF"], t))
        for i, t in enumerate(ranked, 1):
            pos[t][i] += 1
    return pos


class MonteCarloTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(1)
        self.teams = [f"T{i:02d}" for i in range(10)]
        self.base = {t: {"P": 5, "W": 0, "D": 0, "L": 0, "GF": rng.randint(4, 10), "GA": rng.randint(4, 10),
                         "Pts": rng.randint(4, 10)} for t in self.teams}
        for v in self.base.values():
            v["GD"] = v["GF"] - v["GA"]
        self.real = [(a, b, rng.randint(0, 2), rng.randint(0, 2)) for a, b in itertools.permutations(self.teams, 2)
                     if rng.random() < 0.3]
        self.fixtures = [{"home_team": a, "away_team": b, "probs": {"H": 0.45, "D": 0.27, "A": 0.28},
                          "pred_home_goals": 1.4, "pred_away_goals": 1.0}
                         for a, b in itertools.permutations(self.teams, 2) if rng.random() < 0.5]

    def test_deterministic_parity_including_h2h(self):
        fixtures = [dict(f, probs={"H": 1.0, "D": 0.0, "A": 0.0}) for f in self.fixtures]
        for h2h in (False, True):
            _, vp = monte_carlo.simulate_season(self.teams, self.base, fixtures, 3, h2h=h2h, real_matches=self.real, seed=1)
            rp = _ref_positions(self.teams, self.base, fixtures, self.real, 1, h2h, 1)
            for t in self.teams:
                self.assertEqual(set(vp[t]), set(rp[t]), (h2h, t))

    def test_statistical_parity_and_totals(self):
        runs = 4000
        sums, vp = monte_carlo.simulate_season(self.teams, self.base, self.fixtures, runs, h2h=True,
                                               real_matches=self.real, seed=5)
        rp = _ref_positions(self.teams, self.base, self.fixtures, self.real, 2000, True, 9)
        for t in self.teams:
            self.assertAlmostEqual(vp[t][1] / runs, rp[t][1] / 2000, delta=0.05)
            self.assertEqual(sum(vp[t].values()), runs)
            games = sum(1 for f in self.fixtures if t in (f["home_team"], f["away_team"]))
            self.assertEqual(sums[t]["P"] / runs, self.base[t]["P"] + games)

    def test_seed_reproducible_and_tables(self):
        a = monte_carlo.simulate_season(self.teams, self.base, self.fixtures, 50, seed=11, collect_tables=True)
        b = monte_carlo.simulate_season(self.teams, self.base, self.fixtures, 50, seed=11, collect_tables=True)
        self.assertEqual(dict(a[1]["T00"]), dict(b[1]["T00"]))
        self.assertEqual(len(a[2]), 50)


class LigaMxRunCountTests(unittest.TestCase):
    def test_standard_projection_divides_by_its_own_run_count(self):
        """Liga MX ran 20,000 sims but divided by the MLS 500 (title odds 2,148%)."""
        src = (ROOT / "pipelines" / "mls" / "files" / "Project_League_Table.py").read_text(encoding="utf-8")
        start = src.index("teams, table, future_predictions, STANDARD_SIMULATION_RUNS")
        end = src.index('"sim_runs": int(STANDARD_SIMULATION_RUNS)', start)
        block = src[start:end]
        self.assertNotIn("/ SIMULATION_RUNS", block)
        self.assertGreaterEqual(block.count("/ STANDARD_SIMULATION_RUNS"), 15)


class MlsCupPlaceholderTests(unittest.TestCase):
    def test_mls_payload_without_cup_odds(self):
        import league_data as ld

        with mock.patch.object(ld, "_build_mls_winners_odds_bundle", return_value={}):
            out = ld._enrich_mls_payload("United States/MLS", {"format": {}})
        # Used to raise NameError (_empty_mls_cup_odds was missing); the slot
        # is always present (filled from the bracket file when it exists).
        self.assertIn("mls_cup", out["mls_winners_odds"])
        self.assertIn("winners_odds", out["mls_winners_odds"]["mls_cup"])


class SlimUpcomingTests(unittest.TestCase):
    def test_list_row_drops_heavy_fields(self):
        import math_utils
        from routes.predictions import _to_list_upcoming_row

        self.assertFalse(hasattr(math_utils, "_compute_asian_handicap"))
        row = {"competition": "England/Premier League", "home_team": "Arsenal", "away_team": "Chelsea",
               "match_date_iso": _et_day(2), "prob_home": 50.0, "prob_draw": 25.0, "prob_away": 25.0,
               "prob_home_text": "50.0", "correct_score_dist": [1], "last_5_home": [1], "asian_handicap": {"x": 1},
               "total_goals_dist": {}, "prediction_key": "k"}
        out = _to_list_upcoming_row(row)
        for key in ("correct_score_dist", "last_5_home", "asian_handicap", "total_goals_dist",
                    "home_team_info", "away_team_info"):
            self.assertNotIn(key, out)
        self.assertEqual((out["prob_home"], out["prob_home_text"], out["prediction_key"]), (50.0, "50.0", "k"))


class CanonicalCacheTests(unittest.TestCase):
    def test_memo_drops_on_mapping_reload(self):
        import competition_rules as cr

        cr.clear_canonical_team_name_cache()
        m1 = {"Testland/League": {"Old Name": "Canon One"}}
        m2 = {"Testland/League": {"Old Name": "Canon Two"}}
        with mock.patch.object(cr, "_load_display_mapping", return_value=m1):
            self.assertEqual(cr.canonical_team_name("Old Name", "Testland/League"), "Canon One")
            self.assertEqual(cr.canonical_team_name("Old Name", "Testland/League"), "Canon One")
        with mock.patch.object(cr, "_load_display_mapping", return_value=m2):
            self.assertEqual(cr.canonical_team_name("Old Name", "Testland/League"), "Canon Two")
        cr.clear_canonical_team_name_cache()


class StaleWhileRevalidateTests(unittest.TestCase):
    def test_stale_served_then_refreshed(self):
        import cache

        calls = []

        class Req:
            def __init__(self):
                from starlette.datastructures import QueryParams, URL
                self.query_params = QueryParams("")
                self.url = URL("http://t/api/swr-test")

        req = Req()

        def handler():
            from starlette.responses import Response
            calls.append(1)
            return Response(content=f'{{"n": {len(calls)}}}', media_type="application/json")

        wrapped = cache._cached_response(ttl=1)(handler)
        with mock.patch.object(cache, "_extract_request", return_value=req), \
                mock.patch.object(cache, "_redis_client", None):
            first = wrapped()
            self.assertEqual(first.body, b'{"n": 1}')
            key = cache._cache_key("/api/swr-test", "")
            with cache._mem_cache_lock:
                fresh, stale, val = cache._mem_cache[key]
                cache._mem_cache[key] = (time.monotonic() - 1, time.monotonic() + 60, val)
            second = wrapped()
            self.assertEqual(second.body, b'{"n": 1}')  # stale copy served immediately
            self.assertIn("stale", second.headers["X-Cache"])
            for _ in range(50):
                if len(calls) >= 2 and cache._cache_get(key):
                    break
                time.sleep(0.05)
            self.assertEqual(cache._cache_get(key), '{"n": 2}')
            cache._cache_clear_endpoints(["/api/swr-test"])


class PollerSchedulingTests(unittest.TestCase):
    def test_hot_and_idle_competitions(self):
        import live_poller as lp

        now = time.time()
        live_game = {"match_id": "1", "status": "in"}
        soon = {"match_id": "2", "status": "pre", "kickoff_utc": datetime.fromtimestamp(now + 300, timezone.utc).isoformat()}
        later = {"match_id": "3", "status": "pre", "kickoff_utc": datetime.fromtimestamp(now + 4 * 3600, timezone.utc).isoformat()}
        board = {"Live/League": {"games": [live_game]}, "Soon/League": {"games": [soon]}, "Later/League": {"games": [later]},
                 "Idle/League": {"games": []}}
        with mock.patch.dict(lp._live_scores, board, clear=True), mock.patch.dict(lp._comp_last_polled, {
                "Live/League": now - 60, "Soon/League": now - 60, "Later/League": now - 60,
                "Idle/League": now - 60, "Stale/League": now - lp.IDLE_COMP_POLL_SECONDS - 1}, clear=True):
            due = {c for c in ("Live/League", "Soon/League", "Later/League", "Idle/League", "Stale/League", "New/League")
                   if lp._competition_is_due(c, now, {})}
        self.assertEqual(due, {"Live/League", "Soon/League", "Stale/League", "New/League"})


class AccuracyMetricTests(unittest.TestCase):
    def test_scores(self):
        import accuracy_metrics as am

        rows = [
            {"competition": "X", "match_id": "1", "actual_result": "H", "prob_home": 50, "prob_draw": 30, "prob_away": 20},
            {"competition": "X", "match_id": "1", "actual_result": "H", "prob_home": 10, "prob_draw": 10, "prob_away": 80,
             "prematch_prediction": {"prob_home": 0.5, "prob_draw": 0.3, "prob_away": 0.2}},  # duplicate fixture
            {"competition": "X", "match_id": "2", "actual_result": "D", "prob_home": 0.5, "prob_draw": 0.3, "prob_away": 0.2},
            {"competition": "X", "match_id": "3", "actual_result": "", "prob_home": 0.5, "prob_draw": 0.3, "prob_away": 0.2},
            {"competition": "X", "match_id": "4", "actual_result": "A", "schedule_only": "1"},
        ]
        recs = am.settled_predictions(rows)
        self.assertEqual(len(recs), 2)
        s = am.score(recs)
        self.assertEqual((s["n"], s["hit_rate"]), (2, 0.5))
        import math
        self.assertAlmostEqual(s["log_loss"], round((-math.log(0.5) - math.log(0.3)) / 2, 4))
        brier = ((0.5 - 1) ** 2 + 0.3 ** 2 + 0.2 ** 2 + 0.5 ** 2 + (0.3 - 1) ** 2 + 0.2 ** 2) / 2
        self.assertAlmostEqual(s["brier"], round(brier, 4))
        self.assertAlmostEqual(s["draw_actual"], 0.5)

    def test_snapshot_roundtrip(self):
        from shared import sqlite_store

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "a.db"
            sqlite_store.save_accuracy_snapshot(_et_day(0), [{"competition": "ALL", "window": "30d", "n": 3,
                                                              "hit_rate": 0.5, "log_loss": 1.0}], db_path=db)
            hist = sqlite_store.load_accuracy_history("ALL", "30d", db_path=db)
            self.assertEqual((len(hist), hist[0]["n"], hist[0]["log_loss"]), (1, 3, 1.0))


if __name__ == "__main__":
    unittest.main()
