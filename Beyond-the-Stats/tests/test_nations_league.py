"""Tiered Nations Leagues and national-team data integrity.

Regressions (2026-11-17, CONCACAF Nations League):
- one table "Group gro" (first letters of the ``group-stage`` slug) with 37
  teams, counting quarterfinals / Gold Cup play-ins; no tiers; no knockout data;
- national pipeline stored UTC dates (evening games in the Americas a day
  late) -> the same game twice (pipeline row + live row);
- the 45-day archive scan never fetched the first matchday;
- "Türkiye" (live) vs "Turkey" (pipeline) split one country's games;
- postponed games archived as 0-0 results.

ESPN is stubbed; dates are relative to wall-clock time where it matters.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website", ROOT / "shared", ROOT / "pipelines" / "europe" / "files"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import nations_league as nl  # noqa: E402

CNL = "North America/Concacaf Nations League"
UNL = "International/Nations League"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _team(side, name, tid, score=None, winner=None):
    c = {"homeAway": side, "id": tid, "team": {"displayName": name, "id": tid}}
    if score is not None:
        c["score"] = str(score)
    if winner is not None:
        c["winner"] = winner
    return c


def _event(eid, when, home, away, hs=None, as_=None, slug="group-stage", state="post", leg=None,
           note=None, series=None, hwin=None, awin=None):
    comp = {"competitors": [_team("home", home, f"h{home}", hs, hwin), _team("away", away, f"a{away}", as_, awin)]}
    if leg:
        comp["leg"] = {"value": leg}
    if note:
        comp["notes"] = [{"headline": note}]
    if series:
        comp["series"] = series
    return {"id": eid, "date": _iso(when), "season": {"slug": slug},
            "status": {"type": {"state": state, "name": "STATUS_FULL_TIME" if state == "post" else "STATUS_SCHEDULED",
                                "completed": state == "post"}},
            "competitions": [comp]}


class MembershipTests(unittest.TestCase):
    def setUp(self):
        nl._mem.clear()

    def _membership(self, comp, standings):
        with mock.patch.object(nl, "_get_json", return_value=standings), \
                mock.patch.object(nl, "_cache_path", side_effect=lambda n: f"/nonexistent/dir/{n}"):
            return nl.group_membership(comp)

    def test_concacaf_tiers_and_groups(self):
        standings = {"season": {"startDate": "2026-07-01T04:00Z", "endDate": "2027-04-01T03:59Z"}, "children": [
            {"name": "League A, Group A", "standings": {"entries": [{"team": {"displayName": "Costa Rica"}}, {"team": {"displayName": "Haiti"}}]}},
            {"name": "League C, Group B", "standings": {"entries": [{"team": {"displayName": "Aruba"}}]}},
        ]}
        groups = self._membership(CNL, standings)
        self.assertEqual([(g["tier"], g["group"], g["teams"]) for g in groups],
                         [("League A", "Group A", ["Costa Rica", "Haiti"]), ("League C", "Group B", ["Aruba"])])

    def test_uefa_tier_from_group_name_and_canonical_names(self):
        standings = {"children": [{"name": "Group A1", "standings": {"entries": [{"team": {"displayName": "Türkiye"}}]}}]}
        groups = self._membership(UNL, standings)
        self.assertEqual((groups[0]["tier"], groups[0]["group"], groups[0]["teams"]), ("League A", "Group A1", ["Turkey"]))


class KnockoutTieTests(unittest.TestCase):
    def _ties(self, events):
        slim = [nl._slim_event(e, CNL) for e in events]
        with mock.patch.object(nl, "season_events", return_value=slim):
            return nl.knockout_ties(CNL)

    def test_two_legged_ties_aggregate_away_goals_and_live_leg(self):
        t0 = datetime.now(timezone.utc) - timedelta(days=5)
        events = [
            # Aruba v Martinique: 0-0, 1-1 -> Aruba through on away goals (no ESPN series flag).
            _event("1", t0, "Aruba", "Martinique", 0, 0, "gold-cup-play-in-prelims", leg=1),
            _event("2", t0 + timedelta(days=3), "Martinique", "Aruba", 1, 1, "gold-cup-play-in-prelims", leg=2,
                   note="2nd Leg - Tied on aggregate - ARU win on away goals"),
            # Guatemala v Canada: ESPN series winner.
            _event("3", t0, "Guatemala", "Canada", 2, 3, "quarterfinals", leg=1),
            _event("4", t0 + timedelta(days=3), "Canada", "Guatemala", 4, 1, "quarterfinals", leg=2,
                   series={"completed": True, "competitors": [{"id": "hCanada", "winner": True}, {"id": "aGuatemala"}]}),
            # Haiti v USA: second leg live -> counted in the aggregate, no winner yet.
            _event("5", t0, "Haiti", "United States", 0, 6, "quarterfinals", leg=1),
            _event("6", datetime.now(timezone.utc), "United States", "Haiti", 1, 0, "quarterfinals", state="in", leg=2),
            # Group game is not a tie.
            _event("7", t0, "Cuba", "Grenada", 9, 0),
        ]
        ties = self._ties(events)
        self.assertEqual(list(ties), ["gold-cup-play-in-prelims", "quarterfinals"])
        aru = ties["gold-cup-play-in-prelims"][0]
        self.assertEqual((aru["winner"], aru["status"], aru["aggregate"]), ("Aruba", "complete", {"Aruba": 1, "Martinique": 1}))
        qf = {t["home_team"]: t for t in ties["quarterfinals"]}
        self.assertEqual((qf["Guatemala"]["winner"], qf["Guatemala"]["aggregate"]["Canada"]), ("Canada", 7))
        self.assertEqual((qf["Haiti"]["winner"], qf["Haiti"]["status"], qf["Haiti"]["aggregate"]["United States"]),
                         (None, "in_progress", 7))

    def test_penalties_decide_level_tie(self):
        t0 = datetime.now(timezone.utc) - timedelta(days=5)
        events = [
            _event("1", t0, "A", "B", 1, 0, "quarterfinals", leg=1),
            _event("2", t0 + timedelta(days=3), "B", "A", 1, 0, "quarterfinals", leg=2, hwin=True, awin=False),
        ]
        self.assertEqual(self._ties(events)["quarterfinals"][0]["winner"], "B")


class FixtureDateTests(unittest.TestCase):
    def test_evening_kickoff_uses_eastern_date(self):
        # 01:00 UTC is 20:00 (or 21:00) US Eastern on the previous day.
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=1, minute=0, second=0, microsecond=0)
        ev = nl._slim_event(_event("9", tomorrow, "Canada", "Guatemala", slug="quarterfinals", state="pre"), CNL)
        self.assertEqual(ev["match_date"], (tomorrow - timedelta(days=1)).strftime("%Y-%m-%d"))
        with mock.patch.object(nl, "season_events", return_value=[ev]), mock.patch.object(nl, "team_tiers", return_value={}):
            fx = nl.upcoming_fixtures(CNL)
        self.assertEqual((fx[0]["stage_label"], fx[0]["match_date"]), ("Quarterfinals", ev["match_date"]))


class NationalPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "europe_Process_National_Team_Data", ROOT / "pipelines" / "europe" / "files" / "Process_National_Team_Data.py")
        cls.nat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.nat)

    def test_match_date_et_and_normalize(self):
        self.assertEqual(self.nat.match_date_et("2026-11-17T01:00:00+00:00"), "2026-11-16")
        self.assertEqual(self.nat.match_date_et("2026-11-17T19:00:00+00:00"), "2026-11-17")
        rows = [{"match_datetime_utc": "2026-11-17T01:00:00+00:00", "match_date": "2026-11-17"},
                {"match_datetime_utc": "2026-11-17", "match_date": "2026-11-17"}]  # bare date: untouched
        self.assertEqual(self.nat.normalize_match_dates(rows), 1)
        self.assertEqual([r["match_date"] for r in rows], ["2026-11-16", "2026-11-17"])

    def test_merge_takes_result_for_unplayed_row_but_never_rewrites_results(self):
        existing = [{"match_id": "1", "FTR": "", "match_datetime_utc": "a"},
                    {"match_id": "2", "FTR": "H", "FTHG": 2, "match_datetime_utc": "b"}]
        incoming = [{"match_id": "1", "FTR": "A", "match_datetime_utc": "a"},
                    {"match_id": "2", "FTR": "D", "FTHG": 1, "match_datetime_utc": "b"}]
        merged, added = self.nat.merge_national_match_rows(existing, incoming)
        by = {r["match_id"]: r for r in merged}
        self.assertEqual((by["1"]["FTR"], by["2"]["FTR"], added), ("A", "H", 1))

    def test_shared_national_aliases(self):
        from competition_rules import canonical_team_name

        self.assertEqual(self.nat.canonical_team_name("Türkiye"), "Turkey")
        self.assertEqual(canonical_team_name("Türkiye", UNL), "Turkey")
        self.assertEqual(canonical_team_name("Korea Republic", "International/Friendly"), "South Korea")


class ResultIntegrityTests(unittest.TestCase):
    def test_postponed_games_are_not_results(self):
        from predictions import _live_game_to_past_row

        base = {"home_team": "Las Palmas", "away_team": "Eldense", "competition": "Spain/La Liga 2",
                "home_score": 0, "away_score": 0, "match_date": "2026-11-08"}
        self.assertIsNone(_live_game_to_past_row({**base, "status": "post", "status_type": "STATUS_POSTPONED"}))
        self.assertIsNone(_live_game_to_past_row({**base, "status": "post", "status_type": "STATUS_CANCELED"}))
        self.assertIsNotNone(_live_game_to_past_row({**base, "status": "post", "status_type": "STATUS_FULL_TIME"}))

    def test_collector_dedupes_same_match_id(self):
        from competition_rules import _append_game

        games, seen = [], set()
        base = {"competition": CNL, "home_team": "Canada", "away_team": "Guatemala", "home_score": 4,
                "away_score": 1, "match_id": "401929770"}
        _append_game(games, seen, {**base, "match_date": "2026-11-17"})  # old UTC-dated copy
        _append_game(games, seen, {**base, "match_date": "2026-11-16"})
        self.assertEqual(len(games), 1)

    def test_tiered_table_excludes_knockout_and_cross_group_games(self):
        import standings as st

        groups = [{"name": "League A, Group A", "tier": "League A", "group": "Group A", "teams": ["Haiti", "Costa Rica"]}]
        games = [
            {"competition": CNL, "home_team": "Haiti", "away_team": "Costa Rica", "home_score": 2, "away_score": 1,
             "status": "post", "match_date": "2026-09-24", "stage": "group-stage"},
            {"competition": CNL, "home_team": "Haiti", "away_team": "United States", "home_score": 0, "away_score": 6,
             "status": "post", "match_date": "2026-11-14", "stage": "quarterfinals"},
        ]
        with mock.patch("nations_league.group_membership", return_value=groups), \
                mock.patch.object(st, "collect_competition_games", return_value=games), \
                mock.patch.object(st, "filter_games_to_active_season", side_effect=lambda g, c, **k: g):
            table = st._compute_standings_from_history(CNL, force_recompute=True)
        grp = table["groups"][0]
        self.assertEqual((grp["name"], grp["tier"]), ("League A, Group A", "League A"))
        self.assertEqual({e["team"]: (e["P"], e["Pts"]) for e in grp["entries"]}, {"Haiti": (1, 3), "Costa Rica": (1, 0)})


if __name__ == "__main__":
    unittest.main()
