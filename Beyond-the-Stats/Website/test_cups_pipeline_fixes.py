"""Regression tests for cup ESPN IDs, layouts, and Leagues Cup stage classify."""
from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path


WEBSITE_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEBSITE_DIR.parent
FILES_DIR = ROOT_DIR / "pipelines" / "europe" / "files"
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

        path = ROOT_DIR / "Data" / "Seeds" / "league_teams.json"
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


class CupFormatStyleTests(unittest.TestCase):
    def test_knockout_style_for_domestic_cups(self):
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
            self.assertEqual(cr.cup_format_style_for(comp), cr.CUP_FORMAT_STYLE_KNOCKOUT, comp)
            self.assertTrue(cr.is_cup_competition(comp), comp)
            stages = cr.cup_position_stages_for(comp)
            self.assertEqual(stages[0], cr.CUP_POSITION_STAGE_WINNER, comp)
            self.assertIn(cr.CUP_POSITION_STAGE_FINAL, stages, comp)
            self.assertIn(cr.CUP_POSITION_STAGE_SF, stages, comp)

    def test_table_knockout_for_uefa_and_leagues_cup(self):
        import competition_rules as cr

        self.assertEqual(
            cr.cup_format_style_for("Europe/Champions League"),
            cr.CUP_FORMAT_STYLE_TABLE_KNOCKOUT,
        )
        self.assertEqual(
            cr.cup_format_style_for("North America/Leagues Cup"),
            cr.CUP_FORMAT_STYLE_TABLE_KNOCKOUT,
        )

    def test_league_is_not_a_cup(self):
        import competition_rules as cr

        self.assertIsNone(cr.cup_format_style_for("England/Premier League"))
        self.assertFalse(cr.is_cup_competition("England/Premier League"))

    def test_normalize_cup_stage_keys(self):
        import competition_rules as cr

        self.assertEqual(cr._normalize_cup_stage_key("Semi-finals"), cr.CUP_POSITION_STAGE_SF)
        self.assertEqual(cr._normalize_cup_stage_key("Quarter-finals"), cr.CUP_POSITION_STAGE_QF)
        self.assertEqual(cr._normalize_cup_stage_key("Round of 16"), cr.CUP_POSITION_STAGE_RO16)
        self.assertEqual(cr._normalize_cup_stage_key("Final"), cr.CUP_POSITION_STAGE_FINAL)
        self.assertEqual(cr._normalize_cup_stage_key("Champion"), cr.CUP_POSITION_STAGE_WINNER)


class CupDataPayloadTests(unittest.TestCase):
    def test_rebuild_helpers_exist_and_list_cups(self):
        import cup_data as cd

        self.assertTrue(callable(cd.clear_cup_data_caches))
        self.assertTrue(callable(cd.rebuild_cup_data_caches))
        comps = cd.cup_data_competitions()
        self.assertIn("England/FA Cup", comps)
        self.assertIn("Europe/Champions League", comps)
        self.assertNotIn("England/Premier League", comps)

    def test_clear_removes_disk_and_mem(self):
        import json
        import tempfile
        import cup_data as cd
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "england_fa_cup.json"
            path.write_text(json.dumps({"ok": True}), encoding="utf-8")
            with mock.patch.object(cd.config, "CUP_DATA_DIR", tmp):
                with cd._CUP_DATA_MEM_LOCK:
                    cd._CUP_DATA_MEM["England/FA Cup"] = (9999999999.0, {"ok": True})
                removed = cd.clear_cup_data_caches()
            self.assertEqual(removed, 1)
            self.assertFalse(path.exists())
            with cd._CUP_DATA_MEM_LOCK:
                self.assertNotIn("England/FA Cup", cd._CUP_DATA_MEM)

    def test_warm_cup_data_mem_from_disk(self):
        import json
        import tempfile
        import time
        import cup_data as cd
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            payload = {"ok": True, "competition": "England/FA Cup", "fixtures": []}
            path = Path(tmp) / "england_fa_cup.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(cd.config, "CUP_DATA_DIR", tmp):
                with cd._CUP_DATA_MEM_LOCK:
                    cd._CUP_DATA_MEM.clear()
                loaded = cd.warm_cup_data_mem_from_disk()
                self.assertEqual(loaded, 1)
                cached = cd._load_cup_data_from_cache("England/FA Cup")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.get("competition"), "England/FA Cup")

    def test_json_payload_mtime_cache(self):
        import json
        import tempfile
        from predictions import _load_json_payload, clear_json_payload_cache

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brackets.json"
            path.write_text(json.dumps({"competitions": {"X": {"champion": "A"}}}), encoding="utf-8")
            clear_json_payload_cache()
            first = _load_json_payload(str(path))
            second = _load_json_payload(str(path))
            self.assertIs(first, second)
            clear_json_payload_cache()
            third = _load_json_payload(str(path))
            self.assertIsNot(first, third)
            self.assertEqual(third["competitions"]["X"]["champion"], "A")

    def test_gunicorn_warms_cup_data_mem(self):
        src = Path(__file__).resolve().parents[0].joinpath("gunicorn_config.py").read_text(encoding="utf-8")
        self.assertIn("warm_cup_data_mem_from_disk", src)

    def test_stage_position_odds_from_sim_entry(self):
        import cup_data as cd

        entry = {
            "winner_probabilities": {"Arsenal": 0.20, "Chelsea": 0.10},
            "round_reach_probabilities": {
                "Final": {"Arsenal": 0.35, "Chelsea": 0.25},
                "Semi-finals": {"Arsenal": 0.50, "Chelsea": 0.40},
                "Quarter-finals": {"Arsenal": 0.70, "Chelsea": 0.55},
            },
            "elimination_round_probabilities": {
                "Arsenal": {"Quarter-finals": 0.20, "Semi-finals": 0.15, "Final": 0.15, "Champion": 0.20},
                "Chelsea": {"Quarter-finals": 0.30, "Semi-finals": 0.15, "Final": 0.15, "Champion": 0.10},
            },
            "simulations_run": 100,
            "champion": "Arsenal",
        }
        odds = cd._build_cup_stage_position_odds("England/FA Cup", entry)
        self.assertEqual(odds["semantics"], "reach")
        self.assertIn("Winner", odds["stages"])
        self.assertIn("SF", odds["stages"])
        simple_winner = {r["team"]: r["pct"] for r in odds["simple"]["Winner"]}
        self.assertAlmostEqual(simple_winner["Arsenal"], 66.67, delta=1.0)  # renormalized 20/30
        detailed = {r["team"]: r for r in odds["detailed"]}
        self.assertIn("Arsenal", detailed)
        self.assertIn("Winner", detailed["Arsenal"]["odds"])
        # most_likely uses elimination mass (Chelsea QF 30% highest)
        self.assertEqual(detailed["Chelsea"]["most_likely_position"], "QF")

    def test_knockout_format_block_has_no_table(self):
        import cup_data as cd

        fmt = cd._cup_format_block("England/FA Cup")
        self.assertEqual(fmt["format_style"], "knockout")
        self.assertFalse(fmt["has_table"])
        self.assertEqual(fmt["competition_type"], "cup")
        self.assertTrue(fmt["position_stages"])

    def test_table_knockout_format_block_has_table(self):
        import cup_data as cd

        fmt = cd._cup_format_block("Europe/Champions League")
        self.assertEqual(fmt["format_style"], "table_knockout")
        self.assertTrue(fmt["has_table"])

    def test_pipeline_wires_cup_data_rebuild(self):
        src = (ROOT_DIR / "main" / "Daily_Pipeline.py").read_text(encoding="utf-8")
        self.assertIn("rebuild_cup_data_caches", src)
        src2 = (ROOT_DIR / "main" / "Run_All_Pipeline.py").read_text(encoding="utf-8")
        self.assertIn("rebuild_cup_data_caches", src2)
        self.assertIn("_rebuild_cup_data_caches_after_pipeline", src2)


class CupsFailFastTests(unittest.TestCase):
    def test_cups_last_uses_fail_fast(self):
        import ast

        src = (ROOT_DIR / "main" / "Run_All_Pipeline.py").read_text(encoding="utf-8")
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


class CupWinnerNoneAndOddsTests(unittest.TestCase):
    def test_pick_projected_winner_never_returns_draw(self):
        import Track_Cup_Results as track

        self.assertEqual(
            track._pick_projected_winner("TBD", "Arsenal", {}),
            track.NO_PREDICTION,
        )
        self.assertEqual(
            track._pick_projected_winner("Arsenal", "Chelsea", {}),
            track.NO_PREDICTION,
        )
        idx = {("arsenal", "chelsea"): {"prob_home": 0.55, "prob_draw": 0.2, "prob_away": 0.25}}
        self.assertEqual(
            track._pick_projected_winner("Arsenal", "Chelsea", idx),
            "Arsenal",
        )

    def test_match_payload_uses_none_without_odds(self):
        import Track_Cup_Results as track
        import pandas as pd

        row = pd.Series({
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "predicted_result": "D",
            "schedule_only": "1",
            "prob_home": 0,
            "prob_draw": 0,
            "prob_away": 0,
            "pred_home_goals": None,
            "pred_away_goals": None,
            "actual_result": "",
        })
        payload = track._match_payload(row, "Upcoming", {})
        self.assertEqual(payload["winner"], track.NO_PREDICTION)
        self.assertEqual(payload["predicted_result"], track.NO_PREDICTION)

    def test_cup_table_monte_carlo_position_odds_not_all_100(self):
        import Track_Cup_Results as track
        import pandas as pd
        from unittest import mock

        completed = pd.DataFrame([
            {
                "competition": "Europe/Champions League",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "actual_home_goals": 2,
                "actual_away_goals": 1,
                "actual_result": "H",
                "match_date": "2026-09-01",
                "round": "League Phase",
            },
            {
                "competition": "Europe/Champions League",
                "home_team": "Barcelona",
                "away_team": "Inter",
                "actual_home_goals": 1,
                "actual_away_goals": 1,
                "actual_result": "D",
                "match_date": "2026-09-01",
                "round": "League Phase",
            },
        ])
        upcoming = pd.DataFrame([
            {
                "competition": "Europe/Champions League",
                "home_team": "Arsenal",
                "away_team": "Barcelona",
                "prob_home": 0.4,
                "prob_draw": 0.25,
                "prob_away": 0.35,
                "pred_home_goals": 1,
                "pred_away_goals": 1,
                "predicted_result": "H",
                "match_date": "2026-10-01",
                "round": "League Phase",
            },
            {
                "competition": "Europe/Champions League",
                "home_team": "Chelsea",
                "away_team": "Inter",
                "prob_home": 0.33,
                "prob_draw": 0.3,
                "prob_away": 0.37,
                "pred_home_goals": 1,
                "pred_away_goals": 1,
                "predicted_result": "A",
                "match_date": "2026-10-01",
                "round": "League Phase",
            },
        ])
        with mock.patch.object(track, "CUP_TABLE_SIMULATION_RUNS", 40):
            projected, real = track._build_projected_cup_tables(completed, upcoming)
        self.assertFalse(projected.empty)
        self.assertFalse(real.empty)
        # Real table only has completed games.
        self.assertTrue((real["PlayedReal"] > 0).any())
        # Projected odds should vary across positions (not all 100% on one place).
        sample = projected.iloc[0]
        odds = json.loads(sample["position_odds_json"])
        self.assertGreater(len(odds), 1)
        self.assertFalse(all(float(v) in (0.0, 100.0) for v in odds.values()))
        self.assertGreater(int(sample["sim_runs"]), 1)


class CupDataCondensedWinnersTests(unittest.TestCase):
    def test_condensed_winners_odds(self):
        import cup_data as cd

        rows = cd._condensed_winners_odds([
            {"team": "Arsenal", "win_cup_pct": 22.5, "sf_pct": 40},
            {"team": "NONE", "win_cup_pct": 10},
            {"team": "Chelsea", "win_cup_pct": 11},
        ])
        self.assertEqual([r["team"] for r in rows], ["Arsenal", "Chelsea"])
        self.assertEqual(rows[0]["pct"], 22.5)


class CupTableSeasonBoundsTests(unittest.TestCase):
    def test_uefa_table_season_starts_september(self):
        import Track_Cup_Results as track
        from datetime import date

        start, end = track.cup_table_season_bounds(
            "Europe/Champions League",
            reference_date=date(2026, 9, 18),
        )
        self.assertEqual(start.month, 9)
        self.assertEqual(start.day, 1)
        self.assertEqual(start.year, 2026)
        self.assertEqual(end.month, 5)
        self.assertEqual(end.year, 2027)

    def test_filter_drops_pre_september_uefa_games(self):
        import Track_Cup_Results as track
        import pandas as pd
        from unittest import mock

        frame = pd.DataFrame([
            {
                "competition": "Europe/Champions League",
                "match_date": "2026-08-20",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "actual_home_goals": 1,
                "actual_away_goals": 0,
            },
            {
                "competition": "Europe/Champions League",
                "match_date": "2026-09-17",
                "home_team": "Arsenal",
                "away_team": "Inter",
                "actual_home_goals": 2,
                "actual_away_goals": 1,
            },
            {
                "competition": "England/FA Cup",
                "match_date": "2026-08-20",
                "home_team": "Arsenal",
                "away_team": "Portsmouth",
                "actual_home_goals": 3,
                "actual_away_goals": 0,
            },
        ])
        with mock.patch.object(
            track._season_calendar,
            "european_cup_table_season_bounds",
            return_value=(pd.Timestamp("2026-09-01"), pd.Timestamp("2027-05-31")),
        ):
            filtered = track._filter_frame_to_cup_table_season(frame)
        ucl_dates = set(
            filtered.loc[
                filtered["competition"] == "Europe/Champions League", "match_date"
            ].astype(str)
        )
        self.assertNotIn("2026-08-20", ucl_dates)
        self.assertIn("2026-09-17", ucl_dates)
        # Domestic cups are not table cups — August row kept.
        self.assertTrue(
            (
                (filtered["competition"] == "England/FA Cup")
                & (filtered["match_date"].astype(str) == "2026-08-20")
            ).any()
        )
