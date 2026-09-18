"""Regression tests for cup ESPN IDs, layouts, and Leagues Cup stage classify."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


WEBSITE_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEBSITE_DIR.parent
FILES_DIR = ROOT_DIR / "files"
for path in (WEBSITE_DIR, FILES_DIR, ROOT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class CupLayoutAndStageTests(unittest.TestCase):
    def test_knockout_cups_use_bracket_layout_not_table(self):
        import competition_rules as cr

        for comp in (
            "England/FA Cup",
            "England/League Cup",
            "Spain/Copa del Rey",
            "Germany/DFB-Pokal",
            "France/Coupe de France",
            "Italy/Coppa Italia",
            "United States/US Open Cup",
        ):
            self.assertEqual(
                cr.standings_layout_for(comp),
                cr.STANDINGS_LAYOUT_KNOCKOUT,
                comp,
            )

    def test_uefa_and_leagues_cup_keep_table_layouts(self):
        import competition_rules as cr

        self.assertEqual(
            cr.standings_layout_for("Europe/Champions League"),
            cr.STANDINGS_LAYOUT_LEAGUE_PHASE,
        )
        self.assertEqual(
            cr.standings_layout_for("North America/Leagues Cup"),
            cr.STANDINGS_LAYOUT_LEAGUES_CUP,
        )

    def test_leagues_cup_phase_one_round_labels_are_group_not_knockout(self):
        import competition_rules as cr

        for label in ("Phase One", "Phase 1", "Round 1", "Round 2", "Round 3"):
            stage = cr.classify_match_stage(
                {"round": label},
                "North America/Leagues Cup",
            )
            self.assertEqual(stage, "group", label)

        for label in ("Quarter-finals", "Semi-finals", "Final", "Third Place"):
            stage = cr.classify_match_stage(
                {"round": label},
                "North America/Leagues Cup",
            )
            self.assertEqual(stage, "knockout", label)

    def test_knockout_regex_no_longer_matches_bare_round(self):
        import competition_rules as cr

        self.assertIsNone(cr.KNOCKOUT_ROUND_RE.search("Round 1"))
        self.assertIsNotNone(cr.KNOCKOUT_ROUND_RE.search("Round of 16"))


class CupEspnAndKeyMapTests(unittest.TestCase):
    def test_track_cup_espn_ids_cover_all_domestics(self):
        track = importlib.import_module("Track_Cup_Results")
        keys = track.CUP_ESPN_COMPETITION_KEYS
        self.assertEqual(keys["England/League Cup"], "eng.league_cup")
        self.assertEqual(keys["Italy/Coppa Italia"], "ita.coppa_italia")
        self.assertEqual(keys["United States/US Open Cup"], "usa.open")
        self.assertEqual(keys["Spain/Copa del Rey"], "esp.copa_del_rey")
        self.assertEqual(keys["Germany/DFB-Pokal"], "ger.dfb_pokal")
        self.assertEqual(keys["France/Coupe de France"], "fra.coupe_de_france")

    def test_predict_cups_includes_us_open(self):
        src = (FILES_DIR / "Predict_Upcoming_Cups.py").read_text(encoding="utf-8")
        self.assertIn('"name": "United States/US Open Cup"', src)
        self.assertIn('"espn_id": "usa.open"', src)
        self.assertIn('"United States/US Open Cup"', src.split("AUTO_RESOLVE_CUP_COMPETITIONS")[1].split("}")[0])

    def test_tournament_key_map_has_copa_del_rey_and_league_cup(self):
        import config

        self.assertEqual(config.TOURNAMENT_KEY_MAP["copa-del-rey"], "Spain/Copa del Rey")
        self.assertEqual(config.TOURNAMENT_KEY_MAP["league-cup"], "England/League Cup")
        self.assertEqual(config.TOURNAMENT_KEY_MAP["efl-cup"], "England/League Cup")

    def test_config_live_espn_ids_aligned(self):
        import config

        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["Italy/Coppa Italia"], "ita.coppa_italia")
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["United States/US Open Cup"], "usa.open")
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["England/League Cup"], "eng.league_cup")


class DomesticBracketRoundsTests(unittest.TestCase):
    def test_domestic_bracket_emits_rounds(self):
        import pandas as pd
        track = importlib.import_module("Track_Cup_Results")

        frame = pd.DataFrame([
            {
                "match_date": "2026-09-20",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "predicted_result": "H",
                "prob_home": 0.5,
                "prob_draw": 0.25,
                "prob_away": 0.25,
                "pred_home_goals": 2,
                "pred_away_goals": 1,
                "__status": "Upcoming",
            },
            {
                "match_date": "2026-09-10",
                "home_team": "Liverpool",
                "away_team": "Everton",
                "predicted_result": "H",
                "actual_result": "H",
                "actual_home_goals": 1,
                "actual_away_goals": 0,
                "__status": "Completed",
            },
        ])
        bracket = track._build_domestic_cup_bracket_with_draws(
            "England/FA Cup", frame, {}
        )
        self.assertIn("rounds", bracket)
        self.assertGreaterEqual(len(bracket["rounds"]), 1)
        self.assertEqual(bracket["rounds"][0]["name"], "Upcoming Cup Fixtures")
        self.assertEqual(len(bracket["rounds"][0]["matches"]), 1)


class BundesligaRosterTests(unittest.TestCase):
    def test_league_teams_bundesliga_26_27_short_names(self):
        import json

        path = ROOT_DIR / "Data" / "Predictions" / "league_teams.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        bl = set(data["Germany/Bundesliga"])
        self.assertEqual(len(bl), 18)
        self.assertIn("Hamburg", bl)
        self.assertIn("Schalke 04", bl)
        self.assertIn("Elversberg", bl)
        self.assertIn("Paderborn", bl)
        self.assertNotIn("Wolfsburg", bl)
        self.assertNotIn("Hamburg SV", bl)
        self.assertNotIn("Bayer Leverkusen", bl)


class EuropeanSeasonCsvStandingsTests(unittest.TestCase):
    def test_competition_from_processed_path(self):
        import competition_rules as cr

        root = "/data/Processed_Data"
        path = f"{root}/England/Premier League/premstat2026-27.csv"
        self.assertEqual(
            cr._competition_from_processed_season_path(path, root),
            "England/Premier League",
        )

    def test_iter_current_season_skips_prior_and_dedicated(self):
        import tempfile
        import competition_rules as cr
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Processed_Data"
            pl = root / "England" / "Premier League"
            pl.mkdir(parents=True)
            (pl / "premstat2025-26.csv").write_text(
                "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
                "2026-05-01,Arsenal,Chelsea,1,0,H\n",
                encoding="utf-8",
            )
            (pl / "premstat2026-27.csv").write_text(
                "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
                "2026-08-15,Arsenal,Chelsea,2,1,H\n",
                encoding="utf-8",
            )
            mls = root / "United States" / "MLS"
            mls.mkdir(parents=True)
            (mls / "mlsstat2026.csv").write_text(
                "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
                "2026-03-01,Inter Miami,Atlanta Utd,1,0,H\n",
                encoding="utf-8",
            )
            with mock.patch.object(cr, "_club_season_processed_roots", return_value=[str(root)]):
                pairs = cr._iter_current_club_season_csv_files()
        comps = {c for c, _ in pairs}
        self.assertIn("England/Premier League", comps)
        self.assertNotIn("United States/MLS", comps)
        pl_paths = [p for c, p in pairs if c == "England/Premier League"]
        self.assertEqual(len(pl_paths), 1)
        self.assertTrue(pl_paths[0].endswith("premstat2026-27.csv"))

    def test_batch_load_includes_season_csv_without_duplicating_live(self):
        import tempfile
        import competition_rules as cr
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Processed_Data"
            pl = root / "England" / "Premier League"
            pl.mkdir(parents=True)
            (pl / "premstat2026-27.csv").write_text(
                "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
                "2026-08-15,Arsenal,Chelsea,2,1,H\n"
                "2026-08-22,Liverpool,Everton,3,0,H\n",
                encoding="utf-8",
            )
            live = [
                {
                    "competition": "England/Premier League",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "home_score": 2,
                    "away_score": 1,
                    "status": "post",
                    "match_date": "2026-08-15",
                }
            ]
            with mock.patch.object(cr, "_club_season_processed_roots", return_value=[str(root)]):
                with mock.patch.object(cr, "_find_latest_mls_season_file", return_value=None):
                    with mock.patch.object(cr, "_find_latest_liga_mx_season_file", return_value=None):
                        with mock.patch.object(cr.config, "LIVE_SCORE_HISTORY_FILE", "/no/such/history.json"):
                            with mock.patch.object(cr.config, "PAST_GAMES_FILE", "/no/such/past.json"):
                                with mock.patch.object(cr, "NATIONAL_MATCHES_CSV", "/no/nat.csv"):
                                    with mock.patch.object(cr, "WORLD_CUP_PROJECTION_FILE", "/no/wc.json"):
                                        # Avoid reading real upcoming CSVs
                                        with mock.patch("os.path.exists", side_effect=lambda p: str(p).endswith(".csv") and "premstat" in str(p)):
                                            by_comp = {"England/Premier League": []}
                                            seen = {"England/Premier League": set()}
                                            for g in live:
                                                cr._append_game(by_comp["England/Premier League"], seen["England/Premier League"], g)
                                            for comp, path in cr._iter_current_club_season_csv_files():
                                                cr._batch_mls_or_liga_mx(by_comp, seen, comp, path)
        games = by_comp["England/Premier League"]
        # Arsenal-Chelsea from live kept once; Liverpool-Everton added from CSV.
        pairs = {(g["home_team"], g["away_team"]) for g in games}
        self.assertEqual(len(games), 2)
        self.assertIn(("Arsenal", "Chelsea"), pairs)
        self.assertIn(("Liverpool", "Everton"), pairs)


class LeagueDataCacheRebuildTests(unittest.TestCase):
    def test_clear_and_rebuild_helpers_exist(self):
        import league_data as ld

        self.assertTrue(callable(ld.clear_league_data_caches))
        self.assertTrue(callable(ld.rebuild_league_data_caches))
        comps = ld.league_data_rebuild_competitions()
        self.assertIn("England/Premier League", comps)
        self.assertIn("England/FA Cup", comps)

    def test_clear_removes_disk_and_mem(self):
        import json
        import tempfile
        import league_data as ld
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "england_premier_league.json"
            path.write_text(json.dumps({"ok": True}), encoding="utf-8")
            with mock.patch.object(ld.config, "LEAGUE_DATA_DIR", tmp):
                with ld._LEAGUE_DATA_MEM_LOCK:
                    ld._LEAGUE_DATA_MEM["England/Premier League"] = (9999999999.0, {"ok": True})
                removed = ld.clear_league_data_caches()
            self.assertEqual(removed, 1)
            self.assertFalse(path.exists())
            with ld._LEAGUE_DATA_MEM_LOCK:
                self.assertNotIn("England/Premier League", ld._LEAGUE_DATA_MEM)


class CupsFailFastTests(unittest.TestCase):
    def test_cups_last_uses_fail_fast(self):
        import ast

        src = (ROOT_DIR / "Run_All_Pipeline.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_run_cups_last":
                fn = node
                break
        self.assertIsNotNone(fn)
        text = ast.get_source_segment(src, fn) or ""
        self.assertIn("continue_on_error=False", text)
        self.assertNotIn("continue_on_error=True", text)
        self.assertIn("_StepError", text)


if __name__ == "__main__":
    unittest.main()
