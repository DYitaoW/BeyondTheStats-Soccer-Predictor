"""Regression tests: a finished live game flows into tables, past_games and team form.

Covers:
- A final seen by the live poller is written to SQLite ``past_games`` at once
  (previously only the nightly full pipeline archived it), with canonical
  names, ET match date and ESPN ``match_id``.
- Finals are queued for the result drain no matter which caller saw them
  first (the poller's inner flush used to swallow them, so the outer
  standings trigger received an empty set).
- Post-FT score corrections rewrite past_games and re-queue the table refresh;
  unchanged finals are not re-processed every cycle.
- The drain recomputes real standings and invalidates form / overview /
  response caches; concurrent drains coalesce.
- ``load_team_recent_matches`` matches canonical aliases, reads live history,
  and collapses duplicate rows of the same fixture.
- The upcoming-feed last-5 index overlays settled results on the CSV data.
- ``match_analytics`` last-5 sees the new result after invalidation.
- Endpoint-targeted response-cache clearing and alias-aware overview eviction.
- Team identity by resolved id (team overview passes no competition, so
  "Manchester United" must still find rows stored as "Man United").
- Completed-match readers ignore rows dated in the future, and the MLS
  pipeline parses football-data DD/MM/YYYY dates day-first.

All dates are relative to wall-clock time.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
for p in (ROOT, WEBSITE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_poller as lp
import predictions as preds
import match_analytics
import team_overview
import cache as api_cache
from shared import sqlite_store

COMP = "England/Premier League"


def _kickoff(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%MZ")


def _final(mid: str, home: str, away: str, hs: int, as_: int, hours_ago: float = 3.0) -> dict:
    return {
        "match_id": mid,
        "competition": COMP,
        "home_team": home,
        "away_team": away,
        "home_score": hs,
        "away_score": as_,
        "status": "post",
        "status_detail": "FT",
        "kickoff_utc": _kickoff(hours_ago),
    }


class _TempStoreMixin:
    def _setup_store(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bts_store.db"
        for patch in (
            mock.patch.object(sqlite_store, "_default_db_path", return_value=self.db),
            mock.patch.object(sqlite_store, "_past_games_json", return_value=Path(self.tmp.name) / "pg.json"),
            mock.patch.object(sqlite_store, "_past_games_journal", return_value=Path(self.tmp.name) / "pg.jsonl"),
            mock.patch.object(sqlite_store, "_live_history_json", return_value=Path(self.tmp.name) / "lh.json"),
            mock.patch.object(sqlite_store, "_national_raw_matches_csv", return_value=Path(self.tmp.name) / "none.csv"),
            mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(self.db)}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        sqlite_store.ensure_store()


class PollerFinalizationTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._setup_store()
        self.history: list[dict] = []
        self.saved_scores = dict(lp._live_scores)
        self.addCleanup(self._restore_scores)
        lp._live_scores.clear()
        lp._past_synced_scores.clear()
        lp._pending_result_comps.clear()
        lp._pending_result_teams.clear()

        def _upsert(rows, as_of=None):
            by_id = {g.get("match_id"): g for g in self.history}
            for r in rows:
                by_id[r.get("match_id")] = dict(r)
            self.history[:] = list(by_id.values())
            return {"inserted": 0, "updated": 0, "pruned": 0, "total": len(self.history)}

        for patch in (
            mock.patch.object(lp, "_load_live_score_history", side_effect=lambda *a, **k: list(self.history)),
            mock.patch.object(lp, "_upsert_live_score_history", side_effect=_upsert),
            mock.patch.object(lp, "_track_prediction_results"),
            mock.patch.object(lp, "_clear_standings_cache"),
            mock.patch.object(lp, "_clear_leaders_cache"),
            mock.patch.object(preds, "_build_past_game_prediction_lookup", return_value={}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _restore_scores(self):
        lp._live_scores.clear()
        lp._live_scores.update(self.saved_scores)
        lp._past_synced_scores.clear()
        lp._pending_result_comps.clear()
        lp._pending_result_teams.clear()

    def _set_games(self, *games):
        lp._live_scores[COMP] = {"competition": COMP, "games": [dict(g) for g in games]}

    def _past_rows(self):
        return [r for r in sqlite_store.load_past_games() if r.get("match_id")]

    def test_final_written_to_past_games_immediately(self):
        self._set_games(_final("rf-1", "Arsenal", "Chelsea", 2, 1))
        lp._merge_completed_to_history()
        rows = self._past_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["match_id"], "rf-1")
        self.assertEqual((row["actual_home_goals"], row["actual_away_goals"], row["actual_result"]), (2, 1, "H"))
        self.assertEqual(row["competition"], COMP)
        self.assertEqual(row["match_date_iso"], preds._past_row_date_iso(_final("rf-1", "Arsenal", "Chelsea", 2, 1)))
        self.assertTrue(row.get("sync_key"))

    def test_inner_flush_does_not_swallow_queued_results(self):
        """_merge_poll_results_into_live_scores flushes first; the outer call
        returns an empty set, but the result must still be queued."""
        self._set_games(_final("rf-2", "Arsenal", "Chelsea", 0, 0))
        first = lp._merge_completed_to_history()   # inner flush (return discarded upstream)
        second = lp._merge_completed_to_history()  # outer call in the poller loop
        self.assertEqual(first, {COMP})
        self.assertEqual(second, set())
        self.assertIn(COMP, lp._pending_result_comps)
        self.assertIn("Arsenal", lp._pending_result_teams)
        self.assertIn("Chelsea", lp._pending_result_teams)

    def test_unchanged_final_not_reprocessed_but_correction_is(self):
        self._set_games(_final("rf-3", "Arsenal", "Chelsea", 1, 0))
        lp._merge_completed_to_history()
        lp._pending_result_comps.clear()
        lp._pending_result_teams.clear()
        with mock.patch.object(lp, "_sync_finished_games_to_past_games", wraps=lp._sync_finished_games_to_past_games) as sync:
            lp._merge_completed_to_history()
            sync.assert_not_called()
        self.assertEqual(lp._pending_result_comps, set())

        # VAR / admin correction after full time.
        self._set_games(_final("rf-3", "Arsenal", "Chelsea", 1, 1))
        lp._merge_completed_to_history()
        self.assertIn(COMP, lp._pending_result_comps)
        rows = self._past_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actual_home_goals"], rows[0]["actual_away_goals"], rows[0]["actual_result"]), (1, 1, "D"))

    def test_synthetic_games_never_reach_past_games(self):
        self._set_games(_final("sim-999", "Arsenal", "Chelsea", 3, 0), _final("test-1", "Arsenal", "Chelsea", 1, 0))
        lp._merge_completed_to_history()
        self.assertEqual(self._past_rows(), [])
        self.assertEqual(lp._pending_result_teams, set())

    def test_startup_backfill_syncs_without_requeueing(self):
        games = [_final("rf-4", "Arsenal", "Chelsea", 2, 2, hours_ago=30)]
        lp._sync_backfilled_finals_to_past_games(games)
        self.assertEqual(len(self._past_rows()), 1)
        # Same final then appears in memory: already synced, not re-queued.
        self.history.append(dict(games[0]))
        self._set_games(games[0])
        lp._merge_completed_to_history()
        self.assertEqual(lp._pending_result_comps, set())

    def test_drain_refreshes_standings_and_invalidates_caches(self):
        lp._pending_result_comps.update({COMP})
        lp._pending_result_teams.update({"Arsenal", "Chelsea"})
        with mock.patch.object(lp, "refresh_real_standings_after_live_final", return_value=[COMP]) as refresh, \
                mock.patch.object(lp, "_invalidate_result_dependent_caches") as inval:
            out = lp._drain_finalized_results({"Spain/La Liga"})
        self.assertEqual(sorted(c.args[0] for c in refresh.call_args_list), [COMP, "Spain/La Liga"])
        inval.assert_called_once()
        self.assertEqual(inval.call_args.args[0], {"Arsenal", "Chelsea"})
        self.assertEqual(out, [COMP, COMP])
        self.assertEqual(lp._pending_result_comps, set())

    def test_concurrent_drain_coalesces(self):
        lp._result_drain_lock.acquire()
        try:
            with mock.patch.object(lp, "refresh_real_standings_after_live_final") as refresh:
                self.assertEqual(lp._drain_finalized_results({COMP}), [])
                refresh.assert_not_called()
            self.assertIn(COMP, lp._pending_result_comps)  # left for the running drain
        finally:
            lp._result_drain_lock.release()

    def test_invalidation_clears_form_and_analytics(self):
        preds._LAST_5_FORM_CACHE["global"] = {"x": []}
        preds._RECENT_MATCHES_CACHE["Arsenal|5"] = (9e18, [])
        match_analytics._ANALYTICS_HISTORY_CACHE = {"Arsenal": []}
        match_analytics._ANALYTICS_CACHE_EXPIRY = 9e18
        # The leaders clear spawns a background recompute; keep it out of the
        # temp store so it cannot lock the next test's database.
        with mock.patch("team_overview.refresh_team_overviews_after_results") as ov, \
                mock.patch("routes.leagues.clear_league_leaders_cache") as leaders:
            lp._invalidate_result_dependent_caches({"Arsenal"})
        leaders.assert_called_once()
        self.assertEqual(preds._LAST_5_FORM_CACHE, {})
        self.assertEqual(preds._RECENT_MATCHES_CACHE, {})
        self.assertEqual(match_analytics._ANALYTICS_HISTORY_CACHE, {})
        ov.assert_called_once_with(["Arsenal"])


class TeamRecentMatchesTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._setup_store()
        today = datetime.now(ZoneInfo("America/New_York")).date()
        self.d1 = (today - timedelta(days=1)).isoformat()
        self.d8 = (today - timedelta(days=8)).isoformat()

    def test_canonical_alias_live_history_and_duplicates(self):
        base = {
            "match_date_iso": self.d8, "competition": COMP,
            "home_team": "Man United", "away_team": "Arsenal",
            "actual_home_goals": 1, "actual_away_goals": 3, "actual_result": "A",
        }
        sqlite_store.upsert_past_games([
            dict(base, prediction_key="pk-1"),           # prediction:pk-1
            dict(base, source="live_score_history"),      # fixture:... twin
        ])
        # Yesterday's final only in live_score_history (not yet archived).
        sqlite_store.upsert_live_score_history([{
            "match_id": "rf-live-1", "status": "post", "competition": COMP,
            "home_team": "Arsenal", "away_team": "Chelsea", "home_score": 2, "away_score": 0,
            "match_date": self.d1, "game_date": self.d1,
        }])
        canon = sqlite_store._resolve_canonical_team("Man United", COMP)
        for query in {"Man United", canon}:
            got = sqlite_store.load_team_recent_matches(query, limit=5)
            self.assertEqual(len(got), 1, (query, got))
            self.assertTrue(got[0]["is_home"])  # Man United were at home

        arsenal = sqlite_store.load_team_recent_matches("Arsenal", limit=5)
        self.assertEqual([m["date"] for m in arsenal], [self.d1, self.d8])
        self.assertEqual(arsenal[0]["opponent"], "Chelsea")
        self.assertTrue(arsenal[0]["is_home"])
        self.assertFalse(arsenal[1]["is_home"])
        self.assertEqual(arsenal[1]["result"], "A")

    def test_recent_matches_cache_is_per_entry(self):
        preds._RECENT_MATCHES_CACHE.clear()
        stale_ts = 0.0  # epoch: definitely expired
        preds._RECENT_MATCHES_CACHE["Arsenal|5"] = (stale_ts, [{"date": "old"}])
        with mock.patch.object(sqlite_store, "load_team_recent_matches", return_value=[{"date": "new"}]):
            preds._load_team_recent_matches("Chelsea", "/nonexistent", 5)  # fresh entry for another team
            got = preds._load_team_recent_matches("Arsenal", "/nonexistent", 5)
        self.assertEqual(got, [{"date": "new"}])
        preds._RECENT_MATCHES_CACHE.clear()


class Last5OverlayTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._setup_store()
        preds.clear_team_form_caches()
        self.addCleanup(preds.clear_team_form_caches)
        self.today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    def test_overlay_adds_todays_result_under_raw_and_canonical_names(self):
        sqlite_store.upsert_past_games([{
            "match_date_iso": self.today, "competition": COMP,
            "home_team": "Man United", "away_team": "Arsenal",
            "actual_home_goals": 0, "actual_away_goals": 2, "actual_result": "A",
        }])
        idx: dict = {}
        from collections import defaultdict
        idx = defaultdict(list)
        # CSV entry for the same team, same day, different opponent spelling.
        idx["Arsenal"].append({"opponent": "Man Utd", "date": self.today, "result": "H",
                               "home_score": 0, "away_score": 2, "is_home": False})
        preds._overlay_recent_settled_results(idx)
        canon = preds.canonical_team_name("Man United", COMP) or "Man United"
        self.assertTrue(any(m["date"] == self.today for m in idx["Man United"]))
        self.assertTrue(any(m["date"] == self.today for m in idx[canon]))
        merged = preds._last5_for(idx, "Arsenal", "Arsenal")
        self.assertEqual(len([m for m in merged if m["date"] == self.today]), 1)  # deduped by date
        mu = preds._last5_for(idx, "Man Utd", canon)
        self.assertEqual(mu[0]["result"], "A")  # Man United lost (team perspective)
        self.assertTrue(mu[0]["is_home"])

    def test_build_index_includes_overlay_without_processed_dir(self):
        sqlite_store.upsert_past_games([{
            "match_date_iso": self.today, "competition": COMP,
            "home_team": "Arsenal", "away_team": "Chelsea",
            "actual_home_goals": 1, "actual_away_goals": 0, "actual_result": "H",
        }])
        with mock.patch.object(preds.config, "EUROPE_PROCESSED_DIR", "/nonexistent-dir"):
            idx = preds._build_last5_form_index("global")
        self.assertEqual(idx["Arsenal"][0]["date"], self.today)
        self.assertEqual(idx["Chelsea"][0]["result"], "A")


class AnalyticsAndCacheTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._setup_store()
        match_analytics.invalidate_match_history_cache()
        self.addCleanup(match_analytics.invalidate_match_history_cache)

    def test_analytics_reads_store_path_and_sees_new_result_after_invalidation(self):
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        with mock.patch.object(match_analytics.config, "PROJECT_DIR", self.tmp.name), \
                mock.patch.object(match_analytics.config, "EUROPE_PROCESSED_DIR", "/nonexistent", create=True), \
                mock.patch.object(match_analytics.config, "MLS_PROCESSED_DIR", "/nonexistent", create=True), \
                mock.patch.object(match_analytics.config, "EXTRA_PROCESSED_DIR", "/nonexistent", create=True), \
                mock.patch.object(match_analytics.config, "COMPLETED_CUP_FILE", "/nonexistent.csv", create=True):
            older = (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=7)).isoformat()
            sqlite_store.upsert_past_games([{
                "match_date_iso": older, "competition": COMP,
                "home_team": "Everton", "away_team": "Arsenal",
                "actual_home_goals": 1, "actual_away_goals": 1, "actual_result": "D",
            }])
            before = match_analytics.get_team_comprehensive_form("Arsenal", COMP, limit=5)
            self.assertEqual([m["date"] for m in before], [older])
            sqlite_store.upsert_past_games([{
                "match_date_iso": today, "competition": COMP,
                "home_team": "Arsenal", "away_team": "Chelsea",
                "actual_home_goals": 3, "actual_away_goals": 1, "actual_result": "H",
            }])
            # Still cached (TTL) until the result drain invalidates it.
            stale = match_analytics.get_team_comprehensive_form("Arsenal", COMP, limit=5)
            self.assertEqual([m["date"] for m in stale], [older])
            match_analytics.invalidate_match_history_cache()
            after = match_analytics.get_team_comprehensive_form("Arsenal", COMP, limit=5)
        self.assertEqual([m["date"] for m in after], [today, older])
        self.assertEqual(after[0]["result"], "W")

    def test_cache_clear_endpoints_is_targeted(self):
        k_league = api_cache._cache_key("/api/league-data/England/Premier League", "")
        k_notif = api_cache._cache_key("/api/notifications", "")
        api_cache._cache_set(k_league, "{}", ttl=600)
        api_cache._cache_set(k_notif, "{}", ttl=600)
        removed = api_cache._cache_clear_endpoints(("/api/league-data",))
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(api_cache._cache_get(k_league))
        self.assertEqual(api_cache._cache_get(k_notif), "{}")

    def test_overview_eviction_matches_aliases(self):
        payload = {"team": "Arsenal", "canonical_name": "Arsenal", "team_id": "arsenal-id"}
        with team_overview._TEAM_OVERVIEW_CACHE_LOCK:
            team_overview._TEAM_OVERVIEW_CACHE["arsenal fc"] = (9e18, payload)
            team_overview._TEAM_OVERVIEW_CACHE["arsenal"] = (9e18, payload)
            team_overview._TEAM_OVERVIEW_CACHE["chelsea"] = (9e18, {"team": "Chelsea", "team_id": "chelsea-id"})
        try:
            dropped = team_overview.invalidate_team_overviews(["Arsenal"])
            self.assertEqual(dropped, ["Arsenal"])
            self.assertNotIn("arsenal fc", team_overview._TEAM_OVERVIEW_CACHE)
            self.assertNotIn("arsenal", team_overview._TEAM_OVERVIEW_CACHE)
            self.assertIn("chelsea", team_overview._TEAM_OVERVIEW_CACHE)
        finally:
            with team_overview._TEAM_OVERVIEW_CACHE_LOCK:
                for k in ("arsenal fc", "arsenal", "chelsea"):
                    team_overview._TEAM_OVERVIEW_CACHE.pop(k, None)


class TeamIdentityAndFutureGuardTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._setup_store()
        match_analytics.invalidate_match_history_cache()
        self.addCleanup(match_analytics.invalidate_match_history_cache)
        today = datetime.now(ZoneInfo("America/New_York")).date()
        self.past = (today - timedelta(days=3)).isoformat()
        self.future = (today + timedelta(days=20)).isoformat()
        sqlite_store.upsert_past_games([
            {"match_date_iso": self.past, "competition": COMP, "home_team": "Man United",
             "away_team": "Chelsea", "actual_home_goals": 2, "actual_away_goals": 1, "actual_result": "H"},
            # Future-dated "completed" row (swapped date / placeholder friendly).
            {"match_date_iso": self.future, "competition": COMP, "home_team": "Man United",
             "away_team": "Arsenal", "actual_home_goals": 0, "actual_away_goals": 0, "actual_result": "D"},
        ])

    def _ids(self, name):
        return {"Man United": "manchester-united", "Manchester United": "manchester-united",
                "Chelsea": "chelsea", "Arsenal": "arsenal"}.get(str(name).strip(), "")

    def test_recent_matches_by_team_id_and_no_future_rows(self):
        with mock.patch.object(sqlite_store, "_team_identity_id", side_effect=self._ids):
            got = sqlite_store.load_team_recent_matches("Manchester United", limit=5)
        self.assertEqual([m["date"] for m in got], [self.past])
        self.assertTrue(got[0]["is_home"])

    def test_analytics_form_by_team_id_without_competition(self):
        with mock.patch.object(match_analytics.config, "PROJECT_DIR", self.tmp.name), \
                mock.patch.object(match_analytics.config, "EUROPE_PROCESSED_DIR", "/nonexistent", create=True), \
                mock.patch.object(match_analytics.config, "MLS_PROCESSED_DIR", "/nonexistent", create=True), \
                mock.patch.object(match_analytics.config, "EXTRA_PROCESSED_DIR", "/nonexistent", create=True), \
                mock.patch.object(match_analytics.config, "COMPLETED_CUP_FILE", "/nonexistent.csv", create=True), \
                mock.patch.object(sqlite_store, "_team_identity_id", side_effect=self._ids), \
                mock.patch.object(match_analytics, "canonical_team_name", side_effect=lambda n, c="": n):
            form = match_analytics.get_team_comprehensive_form("Manchester United", "", limit=5)
            h2h, _summary = match_analytics.get_head_to_head("Manchester United", "Chelsea")
        self.assertEqual([m["date"] for m in form], [self.past])
        self.assertEqual(form[0]["result"], "W")
        self.assertEqual([m["date"] for m in h2h], [self.past])

    def test_last5_index_drops_future_rows(self):
        preds.clear_team_form_caches()
        self.addCleanup(preds.clear_team_form_caches)
        with mock.patch.object(preds.config, "EUROPE_PROCESSED_DIR", "/nonexistent-dir"):
            idx = preds._build_last5_form_index("global")
        self.assertEqual([m["date"] for m in idx["Man United"]], [self.past])


class MlsDateParsingTests(unittest.TestCase):
    def test_mls_raw_dates_are_day_first(self):
        files_dir = ROOT / "pipelines" / "mls" / "files"
        if str(files_dir) not in sys.path:
            sys.path.insert(0, str(files_dir))
        import importlib.util
        import pandas as pd

        spec = importlib.util.spec_from_file_location("mls_process_data_under_test", files_dir / "Process_Data.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        year = datetime.now(timezone.utc).year
        raw = pd.DataFrame({
            "Date": [f"02/11/{year}", f"12/04/{year}", f"24/10/{year}", f"{year}-03-05"],
            "Home": ["A", "C", "E", "G"], "Away": ["B", "D", "F", "H"],
            "HG": [1, 2, 0, 1], "AG": [0, 2, 1, 1], "Res": ["H", "D", "A", "D"],
        })
        out = mod.convert_mls_raw_to_standard(raw)
        got = dict(zip(out["HomeTeam"], out["Date"].dt.strftime("%Y-%m-%d")))
        self.assertEqual(got["A"], f"{year}-11-02")
        self.assertEqual(got["C"], f"{year}-04-12")
        self.assertEqual(got["E"], f"{year}-10-24")
        self.assertEqual(got["G"], f"{year}-03-05")


if __name__ == "__main__":
    unittest.main()
