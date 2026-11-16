"""Women's pipeline uses REAL ESPN fixtures/results (no generated data).

Regressions from 2026-11-15:
- Womens_Data_Manager generated every season (round-robin fixtures, Poisson
  scores from hard-coded ratings, fake odds) and wrote 1,078 fictional results
  into production past_games; upcoming fixtures did not match the real
  schedule and had no kickoff time.
- competition_rules dropped every past game in which a side scored 0
  (``actual_home_goals or home_score`` turned 0 into None).
- NWSL tables counted playoff games; the projection step served a stale cached
  table; Sort_Data season keys ("2026") broke season parsing and merged leagues.

No network access: ESPN payloads are built inline. Dates are relative.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website", ROOT / "shared", ROOT / "pipelines" / "womens" / "files"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import importlib.util  # noqa: E402

_WOMENS_FILES = ROOT / "pipelines" / "womens" / "files"


def _load_womens_module(name: str):
    """Import a womens pipeline script by path (other pipelines share file names)."""
    spec = importlib.util.spec_from_file_location(f"womens_{name}", _WOMENS_FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wdm = _load_womens_module("Womens_Data_Manager")
from shared import sqlite_store  # noqa: E402

WSL = "England/Women's Super League"
NWSL = "United States/NWSL"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _event(eid, kickoff, home, away, state="post", status="STATUS_FULL_TIME", hs="2", as_="0",
           slug="2026-27-english-womens-super-league", stats=True):
    def comp(side, name, score):
        c = {"homeAway": side, "team": {"displayName": name}, "score": score}
        if stats:
            c["statistics"] = [
                {"name": "totalShots", "displayValue": "11"}, {"name": "shotsOnTarget", "displayValue": "4"},
                {"name": "wonCorners", "displayValue": "6"}, {"name": "foulsCommitted", "displayValue": "9"},
                {"name": "possessionPct", "displayValue": "55.5"},
            ]
        return c
    return {
        "id": eid, "date": _iso(kickoff), "season": {"slug": slug},
        "status": {"type": {"state": state, "name": status, "completed": state == "post"}},
        "competitions": [{"competitors": [comp("home", home, hs), comp("away", away, as_)]}],
    }


class EspnEventParsingTests(unittest.TestCase):
    def test_completed_game_row_uses_real_names_scores_stats_and_et_date(self):
        ko = datetime(2026, 11, 15, 16, 30, tzinfo=timezone.utc)
        row = wdm.event_to_raw_row(_event("401902864", ko, "Arsenal", "London City Lionesses", hs="1", as_="1"), WSL, 2026)
        self.assertEqual(row["Home"], "Arsenal Women")  # canonical women's name
        self.assertEqual(row["Away"], "London City Lionesses")
        self.assertEqual((row["HG"], row["AG"], row["Res"]), (1, 1, "D"))
        self.assertEqual(row["Date"], "15/11/2026")
        self.assertEqual(row["Time"], "11:30")  # US Eastern
        self.assertEqual(row["KickoffUTC"], "2026-11-15T16:30:00Z")
        self.assertEqual(row["MatchId"], "401902864")
        self.assertEqual((row["HS"], row["HST"], row["HC"], row["HPoss"]), (11, 4, 6, 55.5))
        self.assertEqual(row["Stage"], "regular")

    def test_late_us_kickoff_keeps_the_eastern_date(self):
        ko = datetime(2026, 9, 6, 0, 30, tzinfo=timezone.utc)  # 20:30 ET on Sep 5
        row = wdm.event_to_raw_row(_event("1", ko, "Gotham FC", "Portland Thorns FC", slug="regular-season"), NWSL, 2026)
        self.assertEqual(row["Date"], "05/09/2026")
        self.assertEqual((row["Home"], row["Away"]), ("NJ/NY Gotham FC", "Portland Thorns"))

    def test_scheduled_postponed_and_playoff_games(self):
        future = datetime.now(timezone.utc) + timedelta(days=6)
        pre = wdm.event_to_raw_row(_event("2", future, "Chelsea", "Liverpool", state="pre",
                                          status="STATUS_SCHEDULED", hs="0", as_="0"), WSL, 2026)
        self.assertEqual((pre["HG"], pre["AG"], pre["Res"]), ("", "", ""))
        self.assertIsNone(wdm.event_to_raw_row(
            _event("3", future, "Chelsea", "Everton", state="post", status="STATUS_POSTPONED"), WSL, 2026))
        po = wdm.event_to_raw_row(_event("4", future, "Orlando Pride", "Bay FC", state="pre", status="STATUS_SCHEDULED",
                                         slug="playoffs---semifinals"), NWSL, 2026)
        self.assertEqual(po["Stage"], "playoffs")

    def test_season_labels(self):
        self.assertEqual(wdm.LEAGUES[WSL]["season_of"](datetime(2027, 3, 1).date()), 2026)
        self.assertEqual(wdm.LEAGUES[WSL]["season_of"](datetime(2026, 9, 1).date()), 2026)
        self.assertEqual(wdm.LEAGUES[NWSL]["season_of"](datetime(2026, 3, 1).date()), 2026)


class BuildLeagueDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "Raw_Data" / "England" / "WSL"
        self.cfg = dict(wdm.LEAGUES[WSL], raw_dir=self.raw)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_real_csv_and_removes_stale_generated_files(self):
        self.raw.mkdir(parents=True)
        (self.raw / "wslstat2019.csv").write_text("generated")
        season = wdm.current_season(WSL)
        ko = datetime.now(timezone.utc) - timedelta(days=3)
        events = [_event("10", ko, "Arsenal", "Chelsea"),
                  _event("11", ko + timedelta(days=10), "Chelsea", "Arsenal", state="pre",
                         status="STATUS_SCHEDULED", hs="0", as_="0")]
        with mock.patch.dict(wdm.LEAGUES, {WSL: self.cfg}), \
                mock.patch.object(wdm, "fetch_season_events", side_effect=lambda c, s, client=None: events if s == season else []):
            out = wdm.build_league_data(WSL, first_season=season - 1)
        self.assertEqual(out["seasons"], {season: 2})
        self.assertTrue((self.raw / f"wslstat{season}.csv").is_file())
        self.assertFalse((self.raw / "wslstat2019.csv").exists())

    def test_current_season_failure_raises_instead_of_using_stale_data(self):
        season = wdm.current_season(WSL)
        with mock.patch.dict(wdm.LEAGUES, {WSL: self.cfg}), \
                mock.patch.object(wdm, "fetch_season_events", side_effect=RuntimeError("ESPN down")):
            with self.assertRaises(RuntimeError):
                wdm.build_league_data(WSL, first_season=season)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(Path(self.tmp.name) / "s.db")})
        self.env.start()
        sqlite_store.ensure_store()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _past(self):
        # Read the test store directly (load_past_games also merges on-disk files).
        import json
        conn = sqlite_store._open_ready(None)
        try:
            return [json.loads(r["payload_json"]) for r in conn.execute("SELECT payload_json FROM past_games")]
        finally:
            conn.close()

    def _upcoming_keys(self):
        conn = sqlite_store._open_ready(None)
        try:
            return [r[0] for r in conn.execute(
                "SELECT json_extract(payload_json, '$.prediction_key') FROM upcoming_games")]
        finally:
            conn.close()

    def test_purge_removes_only_generated_rows(self):
        d = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        sqlite_store.upsert_past_games([
            {"prediction_key": "pred:gen", "match_date": d, "competition": WSL, "home_team": "Arsenal Women",
             "away_team": "Chelsea Women", "actual_home_goals": 3, "actual_away_goals": 0, "actual_result": "H",
             "source": "womens"},
            {"match_id": "401", "match_date": d, "competition": WSL, "home_team": "Everton Women",
             "away_team": "Liverpool Women", "actual_home_goals": 0, "actual_away_goals": 0, "actual_result": "D",
             "source": "womens_espn"},
        ])
        f = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        sqlite_store.upsert_upcoming_games([
            {"prediction_key": "pred:gen-up", "match_date": f, "competition": WSL, "home_team": "A Women",
             "away_team": "B Women", "source": "womens"},
            {"prediction_key": "pred:real-up", "match_date": f, "competition": WSL, "home_team": "C Women",
             "away_team": "D Women", "source": "womens", "kickoff_utc": f"{f}T12:00:00Z", "match_id": "402"},
        ], source="womens")
        removed = wdm.purge_generated_rows()
        self.assertEqual((removed["past_games"], removed["upcoming_games"]), (1, 1))
        wsl_past = [g for g in self._past() if g.get("source") in ("womens", "womens_espn")]
        self.assertEqual([g.get("source") for g in wsl_past], ["womens_espn"])
        self.assertEqual([k for k in self._upcoming_keys() if str(k).startswith("pred:")], ["pred:real-up"])
        # Idempotent: nothing more to remove, real rows untouched.
        self.assertEqual(wdm.purge_generated_rows()["upcoming_games"], 0)

    def test_sync_skips_games_the_live_poller_already_stored_and_marks_playoffs(self):
        d = (datetime.now(timezone.utc) - timedelta(days=2))
        sqlite_store.upsert_past_games([{
            "match_id": "500", "match_date": d.strftime("%Y-%m-%d"), "competition": "North America/NWSL",
            "home_team": "Orlando Pride", "away_team": "Bay FC", "actual_home_goals": 1, "actual_away_goals": 0,
            "actual_result": "H", "source": "live_score_history", "prob_home": 0.5,
        }])
        rows = [
            wdm.event_to_raw_row(_event("500", d, "Orlando Pride", "Bay FC", hs="1", as_="0",
                                        slug="playoffs---semifinals"), NWSL, 2026),
            wdm.event_to_raw_row(_event("501", d - timedelta(days=30), "Houston Dash", "Bay FC", hs="0", as_="0",
                                        slug="regular-season"), NWSL, 2026),
        ]
        self.assertEqual(wdm.sync_current_season_results(NWSL, rows), 1)  # only 501
        self.assertEqual(wdm.mark_playoff_rows(rows), 1)
        by_id = {g.get("match_id"): g for g in self._past() if g.get("match_id") in ("500", "501")}
        self.assertEqual(by_id["500"].get("stage"), "playoffs")
        self.assertEqual(by_id["500"].get("prob_home"), 0.5)  # poller row kept
        self.assertEqual((by_id["501"]["actual_home_goals"], by_id["501"].get("has_prediction")), (0, False))


class ZeroScoreTests(unittest.TestCase):
    def test_zero_goals_are_real_scores(self):
        from competition_rules import _score_int

        self.assertEqual(_score_int({"actual_home_goals": 0, "home_score": None}, "actual_home_goals", "home_score"), 0)
        self.assertEqual(_score_int({"actual_home_goals": "0"}, "actual_home_goals", "home_score"), 0)
        self.assertEqual(_score_int({"actual_home_goals": None, "home_score": 2}, "actual_home_goals", "home_score"), 2)
        self.assertEqual(_score_int({"actual_home_goals": float("nan"), "home_score": "3"},
                                    "actual_home_goals", "home_score"), 3)
        with self.assertRaises(ValueError):
            _score_int({"actual_home_goals": "", "home_score": None}, "actual_home_goals", "home_score")


class LeagueNameSplitTests(unittest.TestCase):
    """ESPN live names must fold into the season-CSV names, or the same game is
    counted twice (Real Madrid / Bayern showed an extra game played)."""

    def test_espn_short_names_map_to_csv_names(self):
        from competition_rules import canonical_team_name

        self.assertEqual(canonical_team_name("Racing Santander", "Spain/La Liga"), "Santander")
        self.assertEqual(canonical_team_name("Deportivo", "Spain/La Liga"), "La Coruna")
        self.assertEqual(canonical_team_name("Deportivo La Coruña", "Spain/La Liga"), "La Coruna")
        self.assertEqual(canonical_team_name("Borussia Dortmund", "Germany/Bundesliga"), "Dortmund")
        self.assertEqual(canonical_team_name("borussia Dortmund", "Germany/Bundesliga"), "Dortmund")


class ParserStageTests(unittest.TestCase):
    def test_espn_parser_marks_playoff_games(self):
        from espn_parser import _parse_espn_live_event

        ko = datetime.now(timezone.utc) - timedelta(hours=3)
        ev = _event("600", ko, "Washington Spirit", "Kansas City Current", slug="playoffs---championship")
        ev["status"]["type"].update({"detail": "FT", "shortDetail": "FT"})
        game = _parse_espn_live_event(ev)
        self.assertIsNotNone(game)
        self.assertEqual(game.get("stage"), "playoffs")
        self.assertEqual(game.get("season_slug"), "playoffs---championship")
        reg = _parse_espn_live_event(_event("601", ko, "Arsenal", "Chelsea"))
        self.assertNotIn("stage", reg)


class ProjectionInputTests(unittest.TestCase):
    def test_playoffs_excluded_and_model_probabilities_used(self):
        import pandas as pd
        sys.modules.setdefault("Womens_Data_Manager", wdm)
        plt = _load_womens_module("Project_League_Table")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nwslstat2026.csv"
            future = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%d/%m/%Y")
            future_iso = datetime.strptime(future, "%d/%m/%Y").strftime("%Y-%m-%d")
            pd.DataFrame([
                {"Date": "01/05/2026", "Home": "A", "Away": "B", "HG": 2, "AG": 0, "Res": "H", "Stage": "regular"},
                {"Date": "01/11/2026", "Home": "A", "Away": "B", "HG": 0, "AG": 3, "Res": "A", "Stage": "playoffs"},
                {"Date": future, "Home": "B", "Away": "A", "HG": "", "AG": "", "Res": "", "Stage": "regular"},
            ]).to_csv(path, index=False)
            teams, standings, remaining = plt.load_league_state(
                str(path), NWSL, {("B", "A", future_iso): (0.1, 0.2, 0.7)})
        self.assertEqual(standings["A"]["P"], 1)  # playoff game not counted
        self.assertEqual(standings["A"]["Pts"], 3)
        self.assertEqual(remaining, [("B", "A", 0.1, 0.2, 0.7)])


if __name__ == "__main__":
    unittest.main()
