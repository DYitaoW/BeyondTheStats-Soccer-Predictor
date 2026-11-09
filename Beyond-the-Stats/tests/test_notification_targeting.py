"""Regression tests: pushes and Live Activities reach ONLY followers.

A device receives a game's notifications only if it follows that specific
match (ESPN match_id or sync_key) or one of the two teams. Registered devices
that follow nothing — or follow a similarly named club — get nothing.

All kickoff times are relative to wall-clock time (no hardcoded dates).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
for p in (ROOT, WEBSITE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fastapi.testclient import TestClient

from Website.app import app
import config
import notifications as n
import live_poller as lp
from shared import sqlite_store


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "targeting.db"
        sqlite_store.ensure_store(self.db_path)
        self._orig_default_db = sqlite_store._default_db_path
        sqlite_store._default_db_path = lambda: self.db_path
        self._orig_store = n.sqlite_store
        n.sqlite_store = sqlite_store
        self._orig_broadcast = config.APNS_BROADCAST_ENABLED
        config.APNS_BROADCAST_ENABLED = False
        self._reset()

    def tearDown(self):
        self._reset()
        config.APNS_BROADCAST_ENABLED = self._orig_broadcast
        n.sqlite_store = self._orig_store
        sqlite_store._default_db_path = self._orig_default_db
        self.temp_dir.cleanup()

    def _reset(self):
        with lp._live_scores_lock:
            lp._live_scores.clear()
        with lp._notified_events_lock:
            lp._notified_events.clear()
        with lp._match_tracker_lock:
            lp._match_tracker.clear()
        lp._la_timer_anchors.clear()
        for store in (
            n._apns_notification_queue, n._live_activities, n._match_notification_subscriptions,
            n._match_subscription_events, n._team_notification_subscriptions, n._team_subscription_events,
            n._team_follow_records, n._push_to_start_tokens, n._push_started, n._token_environment_cache,
            n.ios_device_tokens, n.device_tokens, n._device_la_settings, n._la_channels,
            n._channel_activity_devices,
        ):
            store.clear()

    # A scoreboard with several simultaneous games, like a real matchday.
    GAMES = [
        ("401001", "Italy/Serie A", "Inter", "Juventus", "s-inter-juve"),
        ("401002", "United States/MLS", "Inter Miami", "Orlando City", "s-miami-orlando"),
        ("401003", "Spain/LaLiga 2", "Real Sociedad B", "Eibar", "s-sociedadb-eibar"),
        ("401004", "Australia/A-League", "Newcastle Jets", "Sydney FC", "s-jets-sydney"),
        ("401005", "England/Premier League", "Man United", "Arsenal", "s-manu-arsenal"),
        ("401006", "France/Ligue 1", "Paris Saint-Germain", "Lyon", "s-psg-lyon"),
    ]

    def _set_all(self, **overrides):
        with lp._live_scores_lock:
            lp._live_scores.clear()
            for mid, comp, home, away, skey in self.GAMES:
                game = {
                    "match_id": mid, "home_team": home, "away_team": away, "sync_key": skey,
                    "status": "in", "home_score": 0, "away_score": 0, "clock": "20'",
                    "period": "1st Half", "status_type": "STATUS_FIRST_HALF",
                }
                game.update(overrides.get(mid, {}))
                lp._live_scores.setdefault(comp, {"competition": comp, "games": []})["games"].append(game)

    def _goal_everywhere(self):
        """Prime all games, then score a home goal in every game; return {match_id: tokens}."""
        self._set_all()
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()
        self._set_all(**{mid: {"home_score": 1, "clock": "21'"} for mid, *_ in self.GAMES})
        lp._dispatch_match_event_notifications()
        out: dict[str, set[str]] = {}
        for e in n._apns_notification_queue:
            if e.get("type") in ("liveactivity", "broadcast"):
                continue
            out.setdefault(e["match_id"], set()).add(e["token"])
        return out


class TeamTargetingTests(_Base):
    def test_only_followers_of_each_club_are_notified(self):
        n.subscribe_team("fan-inter", "Inter", "Italy/Serie A")
        n.subscribe_team("fan-real-sociedad", "Real Sociedad", "Spain/LaLiga")
        n.subscribe_team("fan-newcastle", "Newcastle", "England/Premier League")
        n.subscribe_team("fan-manu", "Manchester United")  # no competition, full name
        n.subscribe_team("fan-psg", "PSG")                 # abbreviation
        # Registered with notifications on, follows nothing.
        TestClient(app).post("/api/notifications/register", json={"token": "bystander"})

        got = self._goal_everywhere()
        self.assertEqual(got.get("401001"), {"fan-inter"})       # Inter only
        self.assertIsNone(got.get("401002"))                     # Inter Miami: nobody
        self.assertIsNone(got.get("401003"))                     # Real Sociedad B: nobody
        self.assertIsNone(got.get("401004"))                     # Newcastle Jets: nobody
        self.assertEqual(got.get("401005"), {"fan-manu"})        # "Man United" on ESPN
        self.assertEqual(got.get("401006"), {"fan-psg"})         # "Paris Saint-Germain" on ESPN
        self.assertNotIn("bystander", set().union(*got.values()))

    def test_follow_by_catalog_team_id(self):
        n.subscribe_team("fan-id", "", team_id="inter-miami-cf")
        got = self._goal_everywhere()
        self.assertEqual(got.get("401002"), {"fan-id"})
        self.assertIsNone(got.get("401001"))

    def test_unsubscribe_by_other_alias_survives_restart(self):
        n.subscribe_team("fan-x", "Man United", "England/Premier League")
        self.assertTrue(n.unsubscribe_team("fan-x", "Manchester United"))
        self._reset()
        n.init_subscriptions_from_sqlite()
        self.assertEqual(n.for_team_tokens("Man United", competition="England/Premier League"), [])
        self.assertEqual([r for r in sqlite_store.load_all_notification_subs() if r["sub_type"] == "team"], [])

    def test_team_follow_persists_with_team_id(self):
        n.subscribe_team("fan-p", "PSG")
        self._reset()
        n.init_subscriptions_from_sqlite()
        self.assertIn("fan-p", n.for_team_tokens("Paris Saint-Germain", competition="France/Ligue 1"))
        self.assertEqual(n.followed_teams("fan-p")[0]["team_id"], "paris-saint-germain")


class MatchTargetingTests(_Base):
    def test_match_follow_only_gets_that_match(self):
        n.subscribe_match("fan-game", "401005", "")
        got = self._goal_everywhere()
        self.assertEqual(got, {"401005": {"fan-game"}})

    def test_sync_key_follow_is_normalized(self):
        n.subscribe_match("fan-url", "2026-11-08%7Carsenal%7Cchelsea", "")
        self.assertIn("fan-url", n.for_match_tokens("2026-11-08|arsenal|chelsea"))
        n.subscribe_match("fan-upper", "2026-11-08|Arsenal|Chelsea", "")
        self.assertIn("fan-upper", n.for_match_tokens("2026-11-08|arsenal|chelsea"))

    def test_all_competition_spellings_are_reached(self):
        n.subscribe_match("fan-a", "401005", "England/Premier League")
        n.subscribe_match("fan-b", "401005", "Some Display Name")
        n.subscribe_match("fan-c", "401005", "")
        self.assertEqual(set(n.for_match_tokens("401005", "England/Premier League")), {"fan-a", "fan-b", "fan-c"})
        # Different match id with a shared prefix is never included.
        n.subscribe_match("fan-other", "4010051", "")
        self.assertNotIn("fan-other", n.for_match_tokens("401005", "England/Premier League"))

    def test_sync_key_follow_reaches_live_game(self):
        n.subscribe_match("fan-sync", "s-psg-lyon", "")
        got = self._goal_everywhere()
        self.assertEqual(got, {"401006": {"fan-sync"}})

    def test_match_follow_ends_at_full_time_but_team_follow_stays(self):
        n.subscribe_match("fan-once", "401005", "")
        n.subscribe_team("fan-team", "Arsenal", "England/Premier League")
        self._set_all()
        lp._dispatch_match_event_notifications()
        self._set_all(**{"401005": {"status": "post", "clock": "FT", "period": "FT",
                                    "status_type": "STATUS_FULL_TIME"}})
        lp._dispatch_match_event_notifications()
        self.assertEqual(n.for_match_tokens("401005", ""), [])
        self.assertIn("fan-team", n.for_team_tokens("Arsenal", competition="England/Premier League"))


class LiveActivityTargetingTests(_Base):
    def test_activity_updates_only_reach_that_matchs_activities(self):
        n.register("act-manu", "dev-1", "401005", "England/Premier League")
        n.register("act-psg", "dev-2", "401006", "France/Ligue 1")
        self._goal_everywhere()
        la_tokens = {(e["match_id"], e["token"]) for e in n._apns_notification_queue if e.get("type") == "liveactivity"}
        self.assertEqual(la_tokens, {("401005", "act-manu"), ("401006", "act-psg")})

    def test_push_to_start_only_for_followers(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=4)
        pre = {mid: {"status": "pre", "kickoff_utc": _iso(ko), "clock": "", "period": "Scheduled",
                     "home_score": None, "away_score": None} for mid, *_ in self.GAMES}
        for dev in ("team-fan", "match-fan", "inter-fan", "nobody"):
            n.register_push_to_start(f"pts-{dev}", dev)
        n.subscribe_team("team-fan", "Arsenal", "England/Premier League")
        n.subscribe_match("match-fan", "s-psg-lyon", "")
        n.subscribe_team("inter-fan", "Inter", "Italy/Serie A")
        self._set_all(**pre)
        lp._dispatch_live_activity_starts()
        starts = {(e["match_id"], e["token"]) for e in n._apns_notification_queue if e.get("event") == "start"}
        self.assertEqual(starts, {
            ("401005", "pts-team-fan"),
            ("401006", "pts-match-fan"),
            ("401001", "pts-inter-fan"),  # Inter — and NOT Inter Miami (401002)
        })


class EndpointTargetingTests(_Base):
    def setUp(self):
        super().setUp()
        self._orig_token = config.MUTATION_API_TOKEN
        config.MUTATION_API_TOKEN = "targeting-admin"
        self.admin = {"X-Admin-Token": "targeting-admin"}
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        config.MUTATION_API_TOKEN = self._orig_token
        super().tearDown()

    def test_subscribe_endpoints_with_team_id_and_sync_key(self):
        self.client.post("/api/notifications/subscribe", json={"token": "t1", "team_id": "inter-miami-cf"})
        self.client.post("/api/notifications/subscribe", json={"token": "t2", "sync_key": "s-psg-lyon"})
        self.client.post("/api/notifications/register", json={
            "token": "t3", "favorite_teams": [{"team": "Inter", "competition": "Italy/Serie A"}],
        })
        got = self._goal_everywhere()
        self.assertEqual(got, {"401002": {"t1"}, "401006": {"t2"}, "401001": {"t3"}})

        prefs = self.client.get("/api/notifications/preferences?token=t1").json()
        self.assertEqual(prefs["team_ids"], ["inter-miami-cf"])
        prefs = self.client.get("/api/notifications/preferences?token=t2").json()
        self.assertEqual(prefs["followed_matches"], ["s-psg-lyon"])

        res = self.client.post("/api/notifications/unsubscribe", json={"token": "t1", "team_id": "inter-miami-cf"})
        self.assertTrue(res.json()["unsubscribed"])
        self.assertEqual(n.for_team_tokens("Inter Miami", competition="United States/MLS"), [])

    def test_admin_broadcast_requires_target(self):
        n.ios_device_tokens.update({"a", "b", "c"})
        n.subscribe_team("a", "Arsenal", "England/Premier League")
        res = self.client.post("/api/notifications", json={"title": "t", "body": "b"}, headers=self.admin)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(len(n._apns_notification_queue), 0)

        res = self.client.post("/api/notifications", headers=self.admin, json={
            "title": "t", "body": "b", "team": "Arsenal",
        })
        self.assertEqual({e["token"] for e in n._apns_notification_queue}, {"a"})
        n._apns_notification_queue.clear()

        res = self.client.post("/api/notifications", headers=self.admin, json={
            "title": "t", "body": "b", "audience": "all",
        })
        self.assertEqual(res.json()["sent"], len(n.ios_device_tokens))

    def test_test_push_never_fans_out_without_token(self):
        n.ios_device_tokens.update({"a", "b"})
        res = self.client.post("/api/test/notifications/push", headers=self.admin, json={"title": "x"})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
